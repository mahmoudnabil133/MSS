import os


class mscolab_test_settings(object):
    # SQLALCHEMY_DB_URI = 'mysql://user:pass@127.0.0.1/mscolab'

    import logging
    # dir where mss output files are stored
    DATA_DIR = os.path.expanduser("~/mss/colabTestData")
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
