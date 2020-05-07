# -*- coding: utf-8 -*-
"""

    mslib.mscolab.conf.py.example
    ~~~~~~~~~~~~~~~~~~~~

    config for mscolab.

    This file is part of mss.

    :copyright: Copyright 2019 Shivashis Padhi
    :copyright: Copyright 2019-2020 by the mss team, see AUTHORS.
    :license: APACHE-2.0, see LICENSE for details.

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.
"""
import logging
import sys
if 'pytest' in sys.argv[0]:
    from mslib.mscolab._tests.test_conf import mscolab_test_settings as mscolab_settings
else:
    try:
        import mscolab_settings
        logging.info("Using user defined settings from %s", mscolab_settings.__file__)
    except ImportError as ex:
        logging.warning(u"Couldn't import mss_wms_settings (ImportError:'%s'), creating dummy config.", ex)

        class mscolab_settings(object):
            # SQLALCHEMY_DB_URI = 'mysql://user:pass@127.0.0.1/mscolab'
            import os
            import logging
            # dir where mss output files are stored
            DATA_DIR = os.path.expanduser("~/mss/colabdata")
            BASE_DIR = os.path.expanduser("~/mss")
            SQLITE_FILE_PATH = os.path.join(DATA_DIR, 'mscolab.db')

            SQLALCHEMY_DB_URI = 'sqlite:///' + SQLITE_FILE_PATH

            # used to generate and parse tokens
            SECRET_KEY = 'secretkEyu'
            DB_HOST = '127.0.0.1'
            DB_USER = 'user'
            DB_PASSWORD = 'pass'
            DB_NAME = 'test_1'

            # SQLALCHEMY_DB_URI = 'postgresql://{}:{}@{}/{}'.format(DB_USER, DB_PASSWORD, DB_HOST, DB_NAME)

            # mscolab data directory
            MSCOLAB_DATA_DIR = os.path.join(DATA_DIR, 'filedata')
            STUB_CODE = """<?xml version="1.0" encoding="utf-8"?>
            <FlightTrack version="1.7.6">
              <ListOfWaypoints>
                <Waypoint flightlevel="250" lat="67.821" location="Kiruna" lon="20.336">
                  <Comments></Comments>
                </Waypoint>
                <Waypoint flightlevel="250" lat="78.928" location="Ny-Alesund" lon="11.986">
                  <Comments></Comments>
                </Waypoint>
              </ListOfWaypoints>
            </FlightTrack>
            """
