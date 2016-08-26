"""
Provide a test() function that can be called from package __init__.
"""

class TestError(Exception):
    pass


def test(*args, **kwargs):
    """
    Run py.test unit tests for the calling package with specified
    ``args`` and ``kwargs``.

    This temporarily changes to the directory above the installed package
    directory and effectively runs ``py.test <packagename> <args> <kwargs>``.

    If the kwarg ``raise_exception=True`` is provided then any test
    failures will result in an exception being raised.  This can be
    used to make shell-level failure.

    :returns: number of test failures
    """
    # Local imports so that imports only get done when really needed.
    import os
    import inspect
    import pytest
    import contextlib

    # Copied from Ska.File to reduce import footprint and limit to only standard
    # modules.
    @contextlib.contextmanager
    def chdir(dirname=None):
        """
        Context manager to run block within `dirname` directory.  The current
        directory is restored even if the block raises an exception.

        :param dirname: Directory name
        """
        curdir = os.getcwd()
        try:
            if dirname is not None:
                os.chdir(dirname)
            yield
        finally:
            os.chdir(curdir)

    raise_exception = kwargs.pop('raise_exception', False)
    package_from_dir = kwargs.pop('package_from_dir', False)

    calling_frame_record = inspect.stack()[1]  # Only works for stack-based Python
    calling_func_file = calling_frame_record[1]

    if package_from_dir:
        # In this case it is assumed that the module which called this function is
        # located in a directory named by the package that is to be tested.  I.e.
        # chandra_aca/test.py.  However, this is NOT the actual package directory
        # so we have to import the package to get its parent directory.
        import importlib
        package = os.path.basename(os.path.dirname(os.path.abspath(calling_func_file)))
        print('package', package)
        module = importlib.import_module(package)
        calling_func_file = module.__file__
        calling_func_module = module.__name__
    else:
        # In this case the module that called this function is the package __init__.py.
        # We get the module directly without doing another import.
        calling_frame = calling_frame_record[0]
        calling_func_name = calling_frame_record[3]
        calling_func_module = calling_frame.f_globals[calling_func_name].__module__

    pkg_names = calling_func_module.split('.')
    pkg_paths = [os.path.dirname(calling_func_file)] + ['..'] * len(pkg_names)
    pkg_dir = os.path.join(*pkg_names)

    with chdir(os.path.join(*pkg_paths)):
        n_fail = pytest.main([pkg_dir] + list(args), **kwargs)

    if n_fail and raise_exception:
        raise TestError('got {} failure(s)'.format(n_fail))

    return n_fail
