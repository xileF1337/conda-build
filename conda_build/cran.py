"""
Tools for converting Cran packages to conda recipes.
"""

from __future__ import absolute_import, division, print_function

import requests

import keyword
import os
import re
import subprocess
import sys
from collections import defaultdict
from os import makedirs, listdir, getcwd, chdir
from os.path import join, isdir, exists, isfile
from tempfile import mkdtemp
from shutil import copy2
from itertools import chain

from conda.fetch import (download, handle_proxy_407)
from conda.connection import CondaSession
from conda.utils import human_bytes, hashsum_file
from conda.install import rm_rf
from conda.compat import input, configparser, StringIO, string_types, PY3
from conda.config import get_proxy_servers
from conda.cli.common import spec_from_line
from conda_build.utils import tar_xf, unzip
from conda_build.source import SRC_CACHE, apply_patch
from conda_build.build import create_env
from conda_build.config import config

from requests.packages.urllib3.util.url import parse_url

CRAN_META = """\
package:
  name: {packagename}
  version: "{conda_version}"

source:
  fn: {filename}
  url: {cranurl}
  # You can add a hash for the file here, like md5 or sha1
  # md5: 49448ba4863157652311cc5ea4fea3ea
  # sha1: 3bcfbee008276084cbb37a2b453963c61176a322
  # patches:
   # List any patch files here
   # - fix.patch

# build:
  # If this is a new build for the same version, increment the build
  # number. If you do not include this key, it defaults to 0.
  # number: 1

{suggests}
requirements:
  build:
{depends}

  run:
{depends}

test:
  commands:
    # You can put additional test commands to be run here.
    - R -e "library('{cran_packagename}')"

  # You can also put a file called run_test.py, run_test.sh, or run_test.bat
  # in the recipe that will be run at test time.

  # requires:
    # Put any additional test requirements here.

about:
  {home_comment}home:{homeurl}
  license: {license}
  {summary_comment}summary:{summary}

# The original CRAN metadata for this package was:

{cran_metadata}

# See
# http://docs.continuum.io/conda/build.html for
# more information about meta.yaml
"""

CRAN_BUILD_SH = """\
#!/bin/bash

# R refuses to build packages that mark themselves as Priority: Recommended
mv DESCRIPTION DESCRIPTION.old
grep -v '^Priority: ' DESCRIPTION.old > DESCRIPTION

# On OS X, the only way to build packages currently is by having
# DYLD_LIBRARY_PATH set.
export DYLD_LIBRARY_PATH=$PREFIX/lib

R CMD INSTALL --build .

# Add more build steps here, if they are necessary.

# See
# http://docs.continuum.io/conda/build.html
# for a list of environment variables that are set during the build process.
"""

CRAN_BLD_BAT = """\
R CMD INSTALL --build .
if errorlevel 1 exit 1

@rem Add more build steps here, if they are necessary.

@rem See
@rem http://docs.continuum.io/conda/build.html
@rem for a list of environment variables that are set during the build process.
"""

INDENT = '\n    - '

CRAN_KEYS = [
    'Site',
    'Archs',
    'Depends',
    'Enhances',
    'Imports',
    'License',
    'License_is_FOSS',
    'License_restricts_use',
    'LinkingTo',
    'MD5sum',
    'NeedsCompilation',
    'OS_type',
    'Package',
    'Path',
    'Priority',
    'Suggests',
    'Version',

    'Title',
    'Author',
    'Maintainer',
]


# The following base/recommended package names are derived from R's source
# tree (R-3.0.2/share/make/vars.mk).  Hopefully they don't change too much
# between versions.
R_BASE_PACKAGE_NAMES = (
    'base',
    'tools',
    'utils',
    'grDevices',
    'graphics',
    'stats',
    'datasets',
    'methods',
    'grid',
    'splines',
    'stats4',
    'tcltk',
    'compiler',
    'parallel',
)

R_RECOMMENDED_PACKAGE_NAMES = (
    'MASS',
    'lattice',
    'Matrix',
    'nlme',
    'survival',
    'boot',
    'cluster',
    'codetools',
    'foreign',
    'KernSmooth',
    'rpart',
    'class',
    'nnet',
    'spatial',
    'mgcv',
)

# Stolen then tweaked from debian.deb822.PkgRelation.__dep_RE.
VERSION_DEPENDENCY_REGEX = re.compile(
    r'^\s*(?P<name>[a-zA-Z0-9.+\-]{1,})'
    r'(\s*\(\s*(?P<relop>[>=<]+)\s*'
    r'(?P<version>[0-9a-zA-Z:\-+~.]+)\s*\))'
    r'?(\s*\[(?P<archs>[\s!\w\-]+)\])?\s*$'
)

def dict_from_cran_lines(lines):
    d = {}
    for line in lines:
        if not line:
            continue
        (k, v) = line.split(': ')
        d[k] = v
        if k not in CRAN_KEYS:
            print("Warning: Unknown key %s" % k)
    d['orig_lines'] = lines
    return d

def remove_package_line_continuations(chunk):
    """
    >>> chunk = [
        'Package: A3',
        'Version: 0.9.2',
        'Depends: R (>= 2.15.0), xtable, pbapply',
        'Suggests: randomForest, e1071',
        'Imports: MASS, R.methodsS3 (>= 1.5.2), R.oo (>= 1.15.8), R.utils (>=',
        '        1.27.1), matrixStats (>= 0.8.12), R.filesets (>= 2.3.0), ',
        '        sampleSelection, scatterplot3d, strucchange, systemfit',
        'License: GPL (>= 2)',
        'NeedsCompilation: no']
    >>> remove_package_line_continuations(chunk)
    ['Package: A3',
     'Version: 0.9.2',
     'Depends: R (>= 2.15.0), xtable, pbapply',
     'Suggests: randomForest, e1071',
     'Imports: MASS, R.methodsS3 (>= 1.5.2), R.oo (>= 1.15.8), R.utils (>= 1.27.1), matrixStats (>= 0.8.12), R.filesets (>= 2.3.0), sampleSelection, scatterplot3d, strucchange, systemfit, rgl,'
     'License: GPL (>= 2)',
     'NeedsCompilation: no']
    """
    continuation = ' ' * 8
    continued_ix = None
    continued_line = None
    had_continuation = False
    accumulating_continuations = False

    for (i, line) in enumerate(chunk):
        if line.startswith(continuation):
            line = ' ' + line.lstrip()
            if accumulating_continuations:
                assert had_continuation
                continued_line += line
                chunk[i] = None
            else:
                accumulating_continuations = True
                continued_ix = i-1
                continued_line = chunk[continued_ix] + line
                had_continuation = True
                chunk[i] = None
        else:
            if accumulating_continuations:
                assert had_continuation
                chunk[continued_ix] = continued_line
                accumulating_continuations = False
                continued_line = None
                continued_ix = None

    if had_continuation:
        # Remove the None(s).
        chunk = [ c for c in chunk if c ]

    chunk.append('')

    return chunk

def get_package_metadata(args, package, d, data):
    [output_dir] = args.output_dir


def main(args, parser):
    package_dicts = {}

    print("Fetching metadata from %s" % args.cran_url)
    r = requests.get(args.cran_url + "PACKAGES")
    PACKAGES = r.text
    package_list = [remove_package_line_continuations(i.splitlines()) for i in PACKAGES.split('\n\n')]

    cran_metadata = {d['Package'].lower(): d for d in map(dict_from_cran_lines,
        package_list)}

    while args.packages:
        [output_dir] = args.output_dir

        package = args.packages.pop()

        if package.lower() not in cran_metadata:
            sys.exit("Package %s not found" % package)

        cran_package = cran_metadata[package.lower()]

        d = package_dicts.setdefault(package,
            {
                'cran_packagename': package,
                'packagename': 'r-' + package.lower(),
                'depends': '',
                # CRAN doesn't seem to have this metadata :(
                'home_comment': '#',
                'homeurl': '',
                'summary_comment': '#',
                'summary': '',
            })

        if args.version:
            raise NotImplementedError("Package versions from CRAN are not yet implemented")
            [version] = args.version
            d['version'] = version

        d['cran_version'] = cran_package['Version']
        # Conda versions cannot have -. Conda (verlib) will treat _ as a .
        d['conda_version'] = d['cran_version'].replace('-', '_')
        d['filename'] = "{cran_packagename}_{cran_version}.tar.bz2".format(**d)
        d['cranurl'] = args.cran_url + d['filename']

        d['cran_metadata'] = '\n'.join(['# %s' % l for l in
            cran_package['orig_lines'] if l])

        # XXX: We should maybe normalize these
        d['license'] = cran_package.get("License", "None")
        if 'License_is_FOSS' in cran_package:
            d['license'] += ' (FOSS)'
        if cran_package.get('License_restricts_use', None) == 'yes':
            d['license'] += ' (Restricts use)'

        if "Suggests" in cran_package:
            d['suggests'] = "# Suggests: %s" % cran_package['Suggests']
        else:
            d['suggests'] = ''

        # Every package depends on at least R.
        # I'm not sure what the difference between depends and imports is.
        depends = [s.strip() for s in cran_package.get('Depends',
            '').split(',') if s.strip()]
        imports = [s.strip() for s in cran_package.get('Imports',
            '').split(',') if s.strip()]
        links = [s.strip() for s in cran_package.get("LinkingTo",
            '').split(',') if s.strip()]

        deps = []
        dep_dict = {}

        for s in set(chain(depends, imports, links)):
            match = VERSION_DEPENDENCY_REGEX.match(s)
            if not match:
                sys.exit("Could not parse version from dependency of %s: %s" %
                    (package, s))
            name = match.group('name')
            archs = match.group('archs')
            relop = match.group('relop') or ''
            version = match.group('version') or ''
            # If there is a relop there should be a version
            assert not relop or version

            if archs:
                sys.exit("Don't know how to handle archs from dependency of "
                "package %s: %s" % (package, s))

            dep_dict[name] = '{relop}{version}'.format(relop=relop, version=version)

        for name in sorted(dep_dict):
            if name in R_BASE_PACKAGE_NAMES:
                continue
            if name == 'R':
                # Put R first
                deps.insert(0, '    - r {version}'.format(version=dep_dict[name]))
            else:
                conda_name = 'r-' + name.lower()
                deps.append('    - {name} {version}'.format(name=conda_name,
                    version=dep_dict[name]))

        d['depends'] = '\n'.join(deps)

    for package in package_dicts:
        d = package_dicts[package]
        name = d['packagename']
        makedirs(join(output_dir, name))
        print("Writing recipe for %s" % package.lower())
        with open(join(output_dir, name, 'meta.yaml'), 'w') as f:
            f.write(CRAN_META.format(**d))
        with open(join(output_dir, name, 'build.sh'), 'w') as f:
            f.write(CRAN_BUILD_SH.format(**d))
        with open(join(output_dir, name, 'bld.bat'), 'w') as f:
            f.write(CRAN_BLD_BAT.format(**d))

    print("Done")
