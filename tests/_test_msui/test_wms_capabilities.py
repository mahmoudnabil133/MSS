# -*- coding: utf-8 -*-
"""

    tests._test_msui.test_wms_capabilities
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    This module provides pytest functions to tests msui.wms_capabilities

    This file is part of MSS.

    :copyright: Copyright 2017 Joern Ungermann
    :copyright: Copyright 2017-2023 by the MSS team, see AUTHORS.
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

import mock
import pytest

from PyQt5 import QtWidgets, QtTest, QtCore
import mslib.msui.wms_capabilities as wc


class Test_WMSCapabilities:

    @pytest.fixture(autouse=True)
    def setup(self, qapp):
        self.capabilities = mock.Mock()
        self.capabilities.capabilities_document = u"Hölla die Waldfee".encode("utf-8")
        self.capabilities.provider = mock.Mock()
        self.capabilities.identification = mock.Mock()
        self.capabilities.provider.contact = mock.Mock()
        self.capabilities.provider.contact.name = None
        self.capabilities.provider.contact.organization = None
        self.capabilities.provider.contact.email = None
        self.capabilities.provider.contact.address = None
        self.capabilities.provider.contact.postcode = None
        self.capabilities.provider.contact.city = None
        yield
        QtWidgets.QApplication.processEvents()

    def start_window(self):
        self.window = wc.WMSCapabilitiesBrowser(
            url="http://example.com",
            capabilities=self.capabilities)
        QtTest.QTest.qWaitForWindowExposed(self.window)
        QtWidgets.QApplication.processEvents()
        QtTest.QTest.qWait(100)

    def test_window_start(self):
        self.start_window()

    def test_window_contact_none(self):
        self.capabilities.provider.contact = None
        self.start_window()

    def test_switch_view(self):
        self.start_window()
        QtTest.QTest.mouseClick(self.window.cbFullView, QtCore.Qt.LeftButton)
        QtWidgets.QApplication.processEvents()
        QtTest.QTest.mouseClick(self.window.cbFullView, QtCore.Qt.LeftButton)
        QtWidgets.QApplication.processEvents()
