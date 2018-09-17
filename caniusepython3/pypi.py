# Copyright 2014 Google Inc. All rights reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import unicode_literals

from distlib.wheel import Wheel
import packaging.utils
import packaging.version
import requests

import enum
import concurrent.futures
import datetime
import json
import logging
import multiprocessing
import pkgutil
import re

try:
    from functools import lru_cache
except ImportError:
    from backports.functools_lru_cache import lru_cache

PROJECT_NAME = re.compile(r'[\w.-]+')


class CanIUsePython3(enum.Enum):
    def __bool__(self):
        return self not in (self.python2_only, self.unknown)

    unknown = 'unknown (Python 2 only assumed)'
    python2_only = 'Python 2 only'
    python3_supported = 'Python 3 supported'
    python3_only = 'Python 3 only'


def just_name(supposed_name):
    """Strip off any versioning or restrictions metadata from a project name."""
    return PROJECT_NAME.match(supposed_name).group(0).lower()


def manual_overrides():
    """Read the overrides file.

    Read the overrides from cache, if available. Otherwise, an attempt is made
    to read the file as it currently stands on GitHub, and then only if that
    fails is the included file used. The result is cached for one day.
    """
    return _manual_overrides(datetime.date.today())


@lru_cache(maxsize=1)
def _manual_overrides(_cache_date=None):
    """Read the overrides file.

    An attempt is made to read the file as it currently stands on GitHub, and
    then only if that fails is the included file used.
    """
    log = logging.getLogger('ciu')
    request = requests.get("https://raw.githubusercontent.com/brettcannon/"
                           "caniusepython3/master/caniusepython3/overrides.json")
    if request.status_code == 200:
        log.info("Overrides loaded from GitHub and cached")
        overrides = request.json()
    else:
        log.info("Overrides loaded from included package data and cached")
        raw_bytes = pkgutil.get_data(__name__, 'overrides.json')
        overrides = json.loads(raw_bytes.decode('utf-8'))
    return frozenset(map(packaging.utils.canonicalize_name, overrides.keys()))


def _classifiy(project_name, response):
    """Parse JSON response to look for Python 2 / 3 support

    First of all, check 'Programming Language :: Python :: *' classifiers
    for Python 2 and 3 classifiers. In case the project has no classifiers
    with Python versions, the algorithm falls back to wheel file names.

    response["info"]["requires_python"] is ignored for now. Any package with
    requires_python flags will most likely have proper classifiers, too.
    """
    log = logging.getLogger("ciu")
    py2 = py3 = None
    cs = response["info"]["classifiers"]
    has_classifier = any(
        c.startswith('Programming Language :: Python :: ') for c in cs
    )
    if has_classifier:
        # Project has at least one Python version classifier.
        log.debug("%s has Python version classifiers", project_name)
        if 'Programming Language :: Python :: 3 :: Only' in cs:
            log.debug("%s is Python 3 only", project_name)
            return CanIUsePython3.python3_only
        if 'Programming Language :: Python :: 2 :: Only' in cs:
            log.debug("%s is Python 2 only", project_name)
            return CanIUsePython3.python2_only
        # Let's assume that missing Python 2/3 classifier means that project
        # doesn't support that major version.
        py2 = any(
            c.startswith("Programming Language :: Python :: 2") for c in cs
        )
        py3 = any(
            c.startswith("Programming Language :: Python :: 3") for c in cs
        )
    else:
        # No classifiers found, now look for wheels in latest release.
        if not response.get('releases'):
            log.debug('%s has no releases', project_name)
            return CanIUsePython3.unknown

        # Check wheels of the highest version.
        latest = str(max(
            packaging.version.parse(v) for v in response['releases']
        ))
        wheel_names = [
            dl['filename'] for dl in response['releases'][latest]
            if dl['packagetype'] == 'bdist_wheel'
        ]
        if not wheel_names:
            log.debug('%s == %s has no wheels', project_name, latest)
            return CanIUsePython3.unknown

        # Check tags of all wheels of highest version.
        log.debug('Checking wheels %s', ', '.join(wheel_names))
        for wheel_name in wheel_names:
            wheel = Wheel(wheel_name)
            for pyver, abi, arch in wheel.tags:
                if pyver.startswith(('py2', 'cp2')):
                    py2 = True
                elif pyver.startswith(('py3', 'cp3')):
                    py3 = True
            if py2 and py3:
                break

    log.debug("%s has py2 %s, py3 %s", project_name, py2, py3)
    if py3 and py2:
        return CanIUsePython3.python3_supported
    elif py3:
        return CanIUsePython3.python3_only
    elif py2:
        return CanIUsePython3.python2_only
    else:
        # Unknown or unsupported pyver wheel tags.
        return CanIUsePython3.unknown


# Create a shared request session to benefit from persistent connections.
# HTTP keep-alive speeds up py3readiness generator a *lot*.
_session = requests.session()


@lru_cache(maxsize=1024)
def supports_py3(project_name):
    """Check with PyPI if a project supports Python 3."""
    log = logging.getLogger("ciu")
    log.info("Checking {} ...".format(project_name))
    request = _session.get("https://pypi.org/pypi/{}/json".format(project_name))
    if request.status_code >= 400:
        log = logging.getLogger("ciu")
        log.warning("problem fetching {}, assuming ported ({})".format(
                        project_name, request.status_code))
        return CanIUsePython3.python3_supported
    response = request.json()
    return _classifiy(project_name, response)
