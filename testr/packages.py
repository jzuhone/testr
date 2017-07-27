from __future__ import print_function, absolute_import, division

import re
from glob import glob
from fnmatch import fnmatch
import sys
import os
import shutil
import mimetypes

import Ska.File
from Ska.Shell import bash, ShellError, Spawn
from pyyaks.logger import get_logger
from astropy.table import Table

opt = None
logger = None


def get_options():
    """
    Get options.

    :returns: options (argparse object)
    """
    from argparse import ArgumentParser
    parser = ArgumentParser()

    parser.add_argument("--test-spec",
                        help="Test include/exclude specification (default=None)",
                        )
    parser.add_argument("--packages-dir",
                        default="packages",
                        help="Directory containing package tests",
                        )
    parser.add_argument("--outputs-dir",
                        default="outputs",
                        help="Root directory containing all output package test runs",
                        )
    parser.add_argument("--outputs-subdir",
                        help="Directory containing per-run output package test runs",
                        )
    parser.add_argument("--regress-dir",
                        default="regress",
                        help="Directory containing per-run regression files",
                        )
    parser.add_argument('--include',
                        action='append',
                        default=[],
                        dest='includes',
                        help=("Include tests that match glob pattern"),
                        )
    parser.add_argument('--exclude',
                        action='append',
                        default=[],
                        dest='excludes',
                        help=("Exclude tests that match glob pattern"),
                        )
    parser.add_argument("--collect-only",
                        action="store_true",
                        help=('Collect tests but do not run'),
                        )
    parser.add_argument("--packages-repo",
                        default='https://github.com/sot',
                        help=("Base URL for package git repos"),
                        )
    parser.add_argument("--overwrite",
                        action="store_true",
                        help=('Overwrite existing outputs directory instead of deleting'),
                        )
    parser.set_defaults()

    return parser.parse_args()


class Tee(object):
    def __init__(self, name, mode='w'):
        self.fh = open(name, mode)

    def __del__(self):
        self.fh.close()

    def write(self, data):
        self.fh.write(data)
        sys.stdout.write(data)

    def flush(self):
        self.fh.flush()
        sys.stdout.flush()


def box_output(lines, min_width=40):
    width = max(min_width, 8 + max([len(x) for x in lines]))
    logger.info('*' * width)
    fmt = '*** {:' + str(width - 8) + 's} ***'
    for line in lines:
        logger.info(fmt.format(line))
    logger.info('*' * width)
    logger.info('')


def include_test_file(package, test_file):
    path = os.path.join(package, test_file)
    include = any(fnmatch(path, x.strip() + '*') for x in opt.includes)
    exclude = any(fnmatch(path, x.strip() + '*') for x in opt.excludes)

    return include and not exclude


def collect_tests():
    """
    Collect tests
    """
    with Ska.File.chdir(opt.packages_dir):
        packages = [x for x in os.listdir('.') if os.path.isdir(x)]

    tests = {}
    for package in packages:
        tests[package] = []

        in_dir = os.path.join(opt.packages_dir, package)
        out_dir = os.path.abspath(os.path.join(opt.outputs_dir, opt.outputs_subdir, package))
        regress_dir = os.path.abspath(os.path.join(opt.regress_dir, opt.outputs_subdir, package))

        with Ska.File.chdir(in_dir):
            test_files = sorted(glob('test_*')) + sorted(glob('post_*'))
            test_files = [x for x in test_files if x.endswith('.py') or x.endswith('.sh')]

            for test_file in test_files:
                status = 'not run' if include_test_file(package, test_file) else '----'

                if test_file.endswith('.py'):
                    interpreter = 'python'
                elif test_file.endswith('.sh'):
                    interpreter = 'bash'
                else:
                    interpreter = None

                test = {'file': test_file,
                        'status': status,
                        'interpreter': interpreter,
                        'out_dir': out_dir,
                        'regress_dir': regress_dir,
                        'packages_repo': opt.packages_repo,
                        'package': package}

                tests[package].append(test)

    return tests


def run_tests(package, tests):
    # Collect test scripts in package and find the ones that are included
    in_dir = os.path.join(opt.packages_dir, package)

    include_tests = [test for test in tests if test['status'] != '----']
    skipping = '' if include_tests else ': skipping - no included tests'
    box_output(['package {}{}'.format(package, skipping)])

    # If no included tests then print message and bail out
    if not include_tests:
        logger.info('')
        return []

    # Copy all files for package tests.
    out_dir = os.path.join(opt.outputs_dir, opt.outputs_subdir, package)
    if not opt.overwrite and os.path.exists(out_dir):
        logger.info('Removing existing output dir {}'.format(out_dir))
        shutil.rmtree(out_dir)

    logger.info('Copying input tests {} to output dir {}'.format(in_dir, out_dir))
    Spawn().run(['rsync', '-a', in_dir + '/', out_dir, '--exclude=*~'])

    # Now run the tests and collect test status
    with Ska.File.chdir(out_dir):
        for test in include_tests:
            # Make the test keys available in the environment
            env = {'TESTR_{}'.format(str(key).upper()): val
                   for key, val in test.items()}

            interpreter = test['interpreter']

            logger.info('Running {} {} script'.format(interpreter, test['file']))
            logfile = Tee(test['file'] + '.log')

            # Set up the right command for bash.  In the case of a bash script the
            # cmd is the actual bash lines as a single string.  In this way each one
            # gets echoed and run so that an intermediate failure is caught.  For
            # no interpreter assume the file is executable.
            if interpreter == 'bash':
                with open(test['file'], 'r') as fh:
                    cmd = fh.read()
            elif interpreter is None:
                cmd = './' + test['file']
            else:
                cmd = interpreter + ' ' + test['file']

            try:
                bash(cmd, logfile=logfile, env=env)
            except ShellError:
                # Test process returned a non-zero status => Fail
                test['status'] = 'FAIL'
            else:
                test['status'] = 'pass'

    box_output(['{} Test Summary'.format(package)] +
               ['{:20s} {}'.format(test['file'], test['status']) for test in tests])


def get_results_table(tests):
    results = []
    for package in sorted(tests):
        for test in tests[package]:
            results.append((package, test['file'], test['status']))
    out = Table(rows=results, names=('Package', 'Script', 'Status'))
    return out


def make_test_dir():
    test_dir = os.path.join(opt.outputs_dir, opt.outputs_subdir)
    if os.path.exists(test_dir):
        print('WARNING: reusing existing output directory {}\n'.format(test_dir))
        # TODO: maybe make this a raw_input confirmation in production.  Note:
        # logger doesn't exist yet since it logs into test_dir.
    else:
        os.makedirs(test_dir)

    # Make a symlink 'last' to the most recent directory
    with Ska.File.chdir(opt.outputs_dir):
        if os.path.exists('last'):
            os.unlink('last')
        os.symlink(opt.outputs_subdir, 'last')

    return test_dir


def make_regress_files(regress_files, out_dir=None, regress_dir=None, clean=None):
    """
    Copy ``regress_files`` from ``out_dir`` to ``regress_dir``, maintaining the
    relative directory structure.

    The ``clean`` parameter specifies a dict of rules for "cleaning" files so that
    uninteresting diffs are eliminated.  Each dict key is the path name (corresponding
    to ``regress_files``) and the value is a 2-tuple of (match_regex, substitution_string).

    :param regress_files: list of relative path names
    :param out_dir: top-level directory for source of files
    :param regress_dir: top-level directory where files are copied
    :param clean: dict of regex substitution rules

    :returns: None
    """
    if clean is None:
        clean = {}

    # Fall back on environment variables that are defined during package testing.
    if out_dir is None:
        out_dir = os.environ.get('TESTR_OUT_DIR')
    if regress_dir is None:
        regress_dir = os.environ.get('TESTR_REGRESS_DIR')

    # Make the top-level directory where files go
    if not os.path.exists(regress_dir):
        os.makedirs(regress_dir)

    for regress_file in regress_files:

        filetype = mimetypes.guess_type(regress_file)[0]
        infile = os.path.join(out_dir, regress_file)

        if filetype != "image/png":
            with open(infile, 'r') as fh:
                lines = fh.readlines()

            if regress_file in clean:
                for sub_in, sub_out in clean[regress_file]:
                    lines = [re.sub(sub_in, sub_out, x) for x in lines]

        # Might need to make output directory since regress_file can
        # contain directory prefix.
        regress_path = os.path.join(regress_dir, regress_file)
        regress_path_dir = os.path.dirname(regress_path)
        if not os.path.exists(regress_path_dir):
            os.makedirs(regress_path_dir)

        if filetype == "image/png":
            shutil.copy(infile, regress_path)
        else:
            with open(regress_path, 'w') as fh:
                fh.writelines(lines)


def check_files(filename, checks, allows=None, out_dir=None):
    """
    Search for ``checks`` regexes in the output ``filename`` (which may be a glob).

    The ``allows`` parameter specifies a list of regexes that are known/accepted check
    failures and can be ignored even if the line matches a check.  The default bash prompt
    Bash-HH:MM:SS> is always allowed, so no lines that are part of the source bash
    commanding will be flagged.

    If any matches are found then a ValueError exception is raised.

    :param filename: relative path name (glob allowed)
    :param checks: list of regexes to try matching
    :param allows: list of regexes that override checks

    :returns: None
    """
    if allows is None:
        allows = []

    allows.append(r'^Bash-\d\d')

    if out_dir is None:
        out_dir = os.environ.get('TESTR_OUT_DIR')

    matches = []
    for filename in glob(filename):
        with open(os.path.join(out_dir, filename), 'r') as fh:
            lines = fh.readlines()

        for check in checks:
            for index, line in enumerate(lines):
                if re.search(check, line, re.IGNORECASE):
                    if not any(re.search(allow, line) for allow in allows):
                        matches.append('{!r} matched at {}:{} :: {}'
                                       .format(check, filename, index, line.strip()))

    if matches:
        raise ValueError('Found matches in check_files:\n{}'.format('\n'.join(matches)))


def process_opt():
    """
    Process options and make various inplace replacements for downstream
    convenience.
    """
    # Set up directories
    if opt.outputs_subdir is None:
        ska_version = bash('./get_version_id')[0]
        opt.outputs_subdir = ska_version

    if opt.test_spec:
        # This puts regression outputs into a separate sub-directory
        # and reads additional test file include/excludes.
        opt.regress_dir = os.path.join(opt.regress_dir, opt.test_spec)

        with open('test_spec_{}'.format(opt.test_spec), 'r') as fh:
            specs = (line.strip() for line in fh)
            specs = [spec for spec in specs if spec and not spec.startswith('#')]

        for spec in specs:
            if spec:
                if spec.startswith('-'):
                    opt.excludes.append(spec[1:])
                else:
                    opt.includes.append(spec)

    # If opt.includes is not expicitly initialized after processing test_spec (which is
    # optional) then use ['*'] to include all tests
    opt.includes = opt.includes or ['*']


def main():
    global opt, logger
    opt = get_options()
    process_opt()

    test_dir = make_test_dir()

    # TODO: back-version existing test.log file to test.log.N where N is the first
    # available number.
    logger = get_logger(name='run_tests', filename=os.path.join(test_dir, 'test.log'))

    tests = collect_tests()  # dict of (list of tests) keyed by package

    if not opt.collect_only:
        for package in sorted(tests):
            run_tests(package, tests[package])  # updates tests[package] in place

    results = get_results_table(tests)
    box_output(results.pformat(max_lines=-1, max_width=-1))
