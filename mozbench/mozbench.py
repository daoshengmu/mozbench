# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import copy
from dzclient import DatazillaRequest, DatazillaResult
import json
import logging
import marionette
import mozinfo
from mozlog.structured import (
    commandline,
    formatters,
    handlers,
    structuredlog,
)
import moznetwork
from mozprocess import ProcessHandler
import mozrunner
import os
import platform
import re
import sys
import time
import urllib
import wait
import wptserve
from subprocess import call

headers = None
results = None


class ChromeRunner(mozrunner.base.BaseRunner):

    def __init__(self, binary, cmdargs=None, **runner_args):
        mozrunner.base.BaseRunner.__init__(self, **runner_args)

        self.binary = binary
        self.cmdargs = cmdargs or []

    @property
    def command(self):
        return [self.binary] + self.cmdargs

class MarionetteRunner(object):

    def __init__(self, cmdargs=None):
        self.cmdargs = cmdargs or []

    def start(self):
        cmd = ['adb', 'forward', 'tcp:2828', 'tcp:2828']
        p = ProcessHandler(cmd)
        p.run()
        p.wait()

        m = marionette.Marionette('localhost', 2828)
        m.start_session()
        print('navigating to: %s' % self.cmdargs[0])
        m.navigate(self.cmdargs[0])
        m.delete_session()

    def stop(self):
        pass

    def wait(self):
        pass

@wptserve.handlers.handler
def results_handler(request, response):
    global headers
    global results
    headers = request.headers
    results = json.loads(request.POST['results'])

routes = [('POST', '/results', results_handler),
          ('GET', '/*', wptserve.handlers.file_handler)]


def run_command(cmd):
    p = ProcessHandler(cmd)
    p.run()
    p.wait()
    return p.output

def cleanup_installation():
    # First let's get the current folder
    folder_to_remove = os.path.abspath(os.path.dirname(__file__))

    # Let's check the OS and determine which folder to remove
    if mozinfo.os == 'mac':
        folder_to_remove = os.path.join(folder_to_remove, 'Firefox.app')
    else:
        folder_to_remove = os.path.join(folder_to_remove, 'firefox')

    # Remove the folder
    cmd = ['rm', '-rf', folder_to_remove]
    run_command(cmd)


def install_firefox(logger, url):
    logger.debug('installing firefox')
    name, headers = urllib.urlretrieve(url, 'firefox.exe')

    if mozinfo.os == 'mac':
        name, headers = urllib.urlretrieve(url, 'firefox.dmg')

    cmd = ['mozinstall', '-d', '.', name]
    path = run_command(cmd)[0]

    if mozinfo.os == 'win':
        path = 'firefox/firefox.exe'
    elif mozinfo.os == 'linux':
        path = 'firefox/firefox'

    if not os.path.isfile(path):
        logger.error('installation failed: path %s does not exist' % path)
        path = None

    return path


def runtest(logger, runner, timeout):
    global headers
    global results

    headers = None
    results = None
    runner.start()

    try:
        wait.Wait(timeout=timeout).until(lambda: results is not None)
    except wait.TimeoutException:
        logger.error('timed out waiting for results')

    runner.stop()
    runner.wait()

    if results:
        # extract browser version from user-agent
        user_agent = headers['user-agent']
        version = None
        m = re.search('(Firefox/[\d\.]+|Chrome/[\d\.]+)', user_agent)
        if m:
            version = m.groups()[0].split('/')[1]

        return version, copy.copy(results)
    else:
        return None, None


def postresults(logger, browser, branch, version, benchmark, results):

    secret_path = os.path.join(os.path.expanduser('~'), 'datazilla-secret.txt')
    if not os.path.isfile(secret_path):
        logger.error('could not post results to datazilla: secrets file: %s not found' % secret_path)
        return
    with open(secret_path, 'r') as f:
        key, secret = f.read().strip().split(',')

    # TODO: get a real build id
    build_id = '%s' % int(time.time())

    req = DatazillaRequest(
        protocol = 'https',
        host = 'datazilla.mozilla.org',
        project = 'mozbench',
        oauth_key = key,
        oauth_secret = secret,
        machine_name = platform.node(),
        os = mozinfo.os,
        os_version = mozinfo.version,
        platform = mozinfo.processor,
        build_name = browser,
        version = version[:2],  # Chrome's version is too long for datazilla
        branch = branch,
        revision = version,
        id = build_id)

    req.add_datazilla_result(results)
    logger.debug('posting %s %s results to datazilla.mozilla.org' %
                 (browser, version))
    responses = req.submit()
    for resp in responses:
        # TODO: I've seen intermitten 403 Forbidden here, we should have
        #       some retries in that case.
        logger.debug('server response: %d %s %s' %
                     (resp.status, resp.reason, resp.read()))


def cli(args):
    global results

    error = False

    parser = argparse.ArgumentParser()
    parser.add_argument('--firefox-url', help='url to firefox installer',
                        default=None)
    parser.add_argument('--use-marionette', action='store_true',
                        help='Use marionette to run tests on firefox os')
    parser.add_argument('--chrome-path', help='path to chrome executable',
                        default=None)
    parser.add_argument('--post-results', action='store_true',
                        help='if specified, post results to datazilla')
    commandline.add_logging_group(parser)
    args = parser.parse_args(args)

    logging.basicConfig()
    logger = commandline.setup_logging('mozbench', vars(args), {})

    if not args.use_marionette and not args.firefox_url:
        logger.error('you must specify one of --use-marionette or --firefox-url')
        return 1

    # install firefox (if necessary)
    if args.firefox_url:
        firefox_binary = install_firefox(logger, args.firefox_url)
        if firefox_binary is None:
            return 1

    logger.debug('starting webserver')
    static_path = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                               'static'))
    httpd = wptserve.server.WebTestHttpd(host=moznetwork.get_ip(), port=8000,
                                         routes=routes, doc_root=static_path)
    httpd.start()

    httpd_logger = logging.getLogger("wptserve")
    httpd_logger.setLevel(logging.ERROR)

    url_prefix = 'http://' + httpd.host + ':' + str(httpd.port) + '/'

    with open(os.path.join(os.path.dirname(__file__), 'benchmarks.json')) as f:
        benchmarks = json.load(f)

    for benchmark in benchmarks:
        suite = benchmark['suite']
        url = url_prefix + benchmark['url']
        num_runs = benchmark['number_of_runs']
        timeout = benchmark['timeout']
        name = benchmark['name']
        value = benchmark['value']

        if not benchmark['enabled']:
            logger.debug('skipping disabled benchmark: %s' % suite)
            continue

        logger.debug('starting benchmark: %s' % suite)

        # Run firefox
        dzres = DatazillaResult()
        dzres.add_testsuite(suite)
        for i in xrange(0, num_runs):

            logger.debug('firefox run %d' % i)
            if args.use_marionette:
                runner = MarionetteRunner(cmdargs=[url])
            else:
                runner = mozrunner.FirefoxRunner(binary=firefox_binary,
                                                 cmdargs=[url])
            version, results = runtest(logger, runner, timeout)
            if results is None:
                logger.error('no results found')
                error = True
            else:
                for result in results:
                    dzres.add_test_results(suite, result[name], [result[value]])
                logger.debug('firefox results: %s' % json.dumps(results))

        if args.post_results:
            postresults(logger, 'firefox', 'nightly', version, benchmark, dzres)

        # Run chrome (if desired)
        if args.chrome_path is None:
            continue

        dzres = DatazillaResult()
        dzres.add_testsuite(suite)
        for i in xrange(0, num_runs):
            logger.debug('chrome run %d' % i)
            runner = ChromeRunner(binary=args.chrome_path, cmdargs=[url])
            version, results = runtest(logger, runner, timeout)
            if results is None:
                logger.error('no results found')
                error = True
            else:
                for result in results:
                    dzres.add_test_results(suite, result[name], [result[value]])
                logger.debug('chrome results: %s' % json.dumps(results))

        if args.post_results:
            postresults(logger, 'chrome', 'canary', version, benchmark, dzres)

    # Cleanup previously installed Firefox
    cleanup_installation()

    return 0 if not error else 1

if __name__ == "__main__":
    exit(cli(sys.argv[1:]))
