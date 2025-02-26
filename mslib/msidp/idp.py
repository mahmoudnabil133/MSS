# pylint: skip-file
# -*- coding: utf-8 -*-
"""
    mslib.msidp.idp.py
    ~~~~~~~~~~~~~~~~~~

    MSS Identity provider implementation.

    This file is part of MSS.

    :copyright: Copyright 2023 Nilupul Manodya
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
# Additional Info:
# This file is imported from
# https://github.com/IdentityPython/pysaml2/blob/master/example/idp2/idp.py
# and customized as MSS requirements. Pylint has been disabled for this imported file.

# Parts of the code

import argparse
import base64
import ssl
import importlib
import logging
import os
import re
import time
import sys

from mslib import msidp
from http.cookies import SimpleCookie
from hashlib import sha1
from urllib.parse import parse_qs
import saml2.xmldsig as ds

from saml2 import (
    BINDING_HTTP_ARTIFACT,
    BINDING_HTTP_POST,
    BINDING_HTTP_REDIRECT,
    BINDING_PAOS, BINDING_SOAP,
    BINDING_URI,
    server,
    time_util
)
from saml2.authn import is_equal
from saml2.authn_context import (
    PASSWORD,
    UNSPECIFIED,
    AuthnBroker,
    authn_context_class_ref
)
from saml2.httputil import (
    BadRequest,
    NotFound,
    Redirect,
    Response,
    ServiceError,
    Unauthorized,
    get_post,
    geturl
)
from saml2.ident import Unknown
from saml2.metadata import create_metadata_string
from saml2.profile import ecp
from saml2.s_utils import PolicyError, UnknownPrincipal, UnsupportedBinding, exception_trace, rndstr
from saml2.sigver import encrypt_cert_from_item, verify_redirect_signature
from werkzeug.serving import run_simple as WSGIServer

from mslib.msidp.idp_user import EXTRA
from mslib.msidp.idp_user import USERS
from mako.lookup import TemplateLookup
from mslib.mscolab.conf import mscolab_settings

logger = logging.getLogger("saml2.idp")
logger.setLevel(logging.WARNING)

DOCS_SERVER_PATH = os.path.dirname(os.path.abspath(msidp.__file__))
LOOKUP = TemplateLookup(
    directories=[os.path.join(DOCS_SERVER_PATH, "templates"), os.path.join(DOCS_SERVER_PATH, "htdocs")],
    module_directory=os.path.join(mscolab_settings.DATA_DIR, 'msidp_modules'),
    input_encoding="utf-8",
    output_encoding="utf-8",
)


class Cache:
    def __init__(self):
        self.user2uid = {}
        self.uid2user = {}


def _expiration(timeout, tformat="%a, %d-%b-%Y %H:%M:%S GMT"):
    """
    :param timeout:
    :param tformat:
    :return:
    """
    if timeout == "now":
        return time_util.instant(tformat)
    elif timeout == "dawn":
        return time.strftime(tformat, time.gmtime(0))
    else:
        # validity time should match lifetime of assertions
        return time_util.in_a_while(minutes=timeout, format=tformat)


# -----------------------------------------------------------------------------


def dict2list_of_tuples(d):
    return [(k, v) for k, v in d.items()]


# -----------------------------------------------------------------------------


class Service:
    def __init__(self, environ, start_response, user=None):
        self.environ = environ
        logger.debug("ENVIRON: %s", environ)
        self.start_response = start_response
        self.user = user

    def unpack_redirect(self):
        if "QUERY_STRING" in self.environ:
            _qs = self.environ["QUERY_STRING"]
            return {k: v[0] for k, v in parse_qs(_qs).items()}
        else:
            return None

    def unpack_post(self):
        post_data = get_post(self.environ)
        _dict = parse_qs(post_data if isinstance(post_data, str) else post_data.decode("utf-8"))
        logger.debug("unpack_post:: %s", _dict)
        try:
            return {k: v[0] for k, v in _dict.items()}
        except Exception:
            return None

    def unpack_soap(self):
        try:
            query = get_post(self.environ)
            return {"SAMLRequest": query, "RelayState": ""}
        except Exception:
            return None

    def unpack_either(self):
        if self.environ["REQUEST_METHOD"] == "GET":
            _dict = self.unpack_redirect()
        elif self.environ["REQUEST_METHOD"] == "POST":
            _dict = self.unpack_post()
        else:
            _dict = None
        logger.debug("_dict: %s", _dict)
        return _dict

    def operation(self, saml_msg, binding):
        logger.debug("_operation: %s", saml_msg)
        if not (saml_msg and "SAMLRequest" in saml_msg):
            resp = BadRequest("Error parsing request or no request")
            return resp(self.environ, self.start_response)
        else:
            # saml_msg may also contain Signature and SigAlg
            if "Signature" in saml_msg:
                try:
                    kwargs = {
                        "signature": saml_msg["Signature"],
                        "sigalg": saml_msg["SigAlg"],
                    }
                except KeyError:
                    resp = BadRequest("Signature Algorithm specification is missing")
                    return resp(self.environ, self.start_response)
            else:
                kwargs = {}

            try:
                kwargs["encrypt_cert"] = encrypt_cert_from_item(saml_msg["req_info"].message)
            except KeyError:
                pass

            try:
                kwargs["relay_state"] = saml_msg["RelayState"]
            except KeyError:
                pass

            return self.do(saml_msg["SAMLRequest"], binding, **kwargs)

    def artifact_operation(self, saml_msg):
        if not saml_msg:
            resp = BadRequest("Missing query")
            return resp(self.environ, self.start_response)
        else:
            # exchange artifact for request
            request = IdpServerSettings_.IDP.artifact2message(saml_msg["SAMLart"], "spsso")
            try:
                return self.do(request, BINDING_HTTP_ARTIFACT, saml_msg["RelayState"])
            except KeyError:
                return self.do(request, BINDING_HTTP_ARTIFACT)

    def response(self, binding, http_args):
        resp = None
        if binding == BINDING_HTTP_ARTIFACT:
            resp = Redirect()
        elif http_args["data"]:
            resp = Response(http_args["data"], headers=http_args["headers"])
        else:
            for header in http_args["headers"]:
                if header[0] == "Location":
                    resp = Redirect(header[1])

        if not resp:
            resp = ServiceError("Don't know how to return response")

        return resp(self.environ, self.start_response)

    def do(self, query, binding, relay_state="", encrypt_cert=None):
        pass

    def redirect(self):
        """Expects a HTTP-redirect request"""

        _dict = self.unpack_redirect()
        return self.operation(_dict, BINDING_HTTP_REDIRECT)

    def post(self):
        """Expects a HTTP-POST request"""

        _dict = self.unpack_post()
        return self.operation(_dict, BINDING_HTTP_POST)

    def artifact(self):
        # Can be either by HTTP_Redirect or HTTP_POST
        _dict = self.unpack_either()
        return self.artifact_operation(_dict)

    def soap(self):
        """
        Single log out using HTTP_SOAP binding
        """
        logger.debug("- SOAP -")
        _dict = self.unpack_soap()
        logger.debug("_dict: %s", _dict)
        return self.operation(_dict, BINDING_SOAP)

    def uri(self):
        _dict = self.unpack_either()
        return self.operation(_dict, BINDING_SOAP)

    def not_authn(self, key, requested_authn_context):
        ruri = geturl(self.environ, query=False)

        kwargs = dict(authn_context=requested_authn_context, key=key, redirect_uri=ruri)
        # Clear cookie, if it already exists
        kaka = delete_cookie(self.environ, "idpauthn")
        if kaka:
            kwargs["headers"] = [kaka]
        return do_authentication(self.environ, self.start_response, **kwargs)


# -----------------------------------------------------------------------------


REPOZE_ID_EQUIVALENT = "uid"
FORM_SPEC = """<form name="myform" method="post" action="%s">
   <input type="hidden" name="SAMLResponse" value="%s" />
   <input type="hidden" name="RelayState" value="%s" />
</form>"""


# -----------------------------------------------------------------------------
# === Single log in ====
# -----------------------------------------------------------------------------


class AuthenticationNeeded(Exception):
    def __init__(self, authn_context=None, *args, **kwargs):
        Exception.__init__(*args, **kwargs)
        self.authn_context = authn_context


class SSO(Service):
    def __init__(self, environ, start_response, user=None):
        Service.__init__(self, environ, start_response, user)
        self.binding = ""
        self.response_bindings = None
        self.resp_args = {}
        self.binding_out = None
        self.destination = None
        self.req_info = None
        self.op_type = ""

    def verify_request(self, query, binding):
        """
        :param query: The SAML query, transport encoded
        :param binding: Which binding the query came in over
        """
        resp_args = {}
        if not query:
            logger.info("Missing QUERY")
            resp = Unauthorized("Unknown user")
            return resp_args, resp(self.environ, self.start_response)

        if not self.req_info:
            self.req_info = IdpServerSettings_.IDP.parse_authn_request(query, binding)

        logger.info("parsed OK")
        _authn_req = self.req_info.message
        logger.debug("%s", _authn_req)

        try:
            self.binding_out, self.destination = IdpServerSettings_.IDP.pick_binding(
                "assertion_consumer_service",
                bindings=self.response_bindings,
                entity_id=_authn_req.issuer.text,
                request=_authn_req,
            )
        except Exception as err:
            logger.error("Couldn't find receiver endpoint: %s", err)
            raise

        logger.debug("Binding: %s, destination: %s", self.binding_out, self.destination)

        resp_args = {}
        try:
            resp_args = IdpServerSettings_.IDP.response_args(_authn_req)
            _resp = None
        except UnknownPrincipal as excp:
            _resp = IdpServerSettings_.IDP.create_error_response(_authn_req.id, self.destination, excp)
        except UnsupportedBinding as excp:
            _resp = IdpServerSettings_.IDP.create_error_response(_authn_req.id, self.destination, excp)

        return resp_args, _resp

    def do(self, query, binding_in, relay_state="", encrypt_cert=None, **kwargs):
        """
        :param query: The request
        :param binding_in: Which binding was used when receiving the query
        :param relay_state: The relay state provided by the SP
        :param encrypt_cert: Cert to use for encryption
        :return: A response
        """
        try:
            resp_args, _resp = self.verify_request(query, binding_in)
        except UnknownPrincipal as excp:
            logger.error("UnknownPrincipal: %s", excp)
            resp = ServiceError(f"UnknownPrincipal: {excp}")
            return resp(self.environ, self.start_response)
        except UnsupportedBinding as excp:
            logger.error("UnsupportedBinding: %s", excp)
            resp = ServiceError(f"UnsupportedBinding: {excp}")
            return resp(self.environ, self.start_response)

        if not _resp:
            identity = USERS[self.user].copy()
            # identity["eduPersonTargetedID"] = get_eptid(IDP, query, session)
            logger.info("Identity: %s", identity)

            if REPOZE_ID_EQUIVALENT:
                identity[REPOZE_ID_EQUIVALENT] = self.user
            try:
                try:
                    metod = self.environ["idp.authn"]
                except KeyError:
                    pass
                else:
                    resp_args["authn"] = metod

                _resp = IdpServerSettings_.IDP.create_authn_response(
                    identity, userid=self.user, encrypt_cert_assertion=encrypt_cert, **resp_args
                )
            except Exception as excp:
                logging.error(exception_trace(excp))
                resp = ServiceError(f"Exception: {excp}")
                return resp(self.environ, self.start_response)

        logger.info("AuthNResponse: %s", _resp)
        if self.op_type == "ecp":
            kwargs = {"soap_headers": [ecp.Response(
                assertion_consumer_service_url=self.destination)]}
        else:
            kwargs = {}

        http_args = IdpServerSettings_.IDP.apply_binding(
            self.binding_out, f"{_resp}", self.destination, relay_state, response=True, **kwargs
        )

        logger.debug("HTTPargs: %s", http_args)
        return self.response(self.binding_out, http_args)

    @staticmethod
    def _store_request(saml_msg):
        logger.debug("_store_request: %s", saml_msg)
        key = sha1(saml_msg["SAMLRequest"].encode()).hexdigest()
        # store the AuthnRequest
        IdpServerSettings_.IDP.ticket[key] = saml_msg
        return key

    def redirect(self):
        """This is the HTTP-redirect endpoint"""

        logger.info("--- In SSO Redirect ---")
        saml_msg = self.unpack_redirect()

        try:
            _key = saml_msg["key"]
            saml_msg = IdpServerSettings_.IDP.ticket[_key]
            self.req_info = saml_msg["req_info"]
            del IdpServerSettings_.IDP.ticket[_key]
        except KeyError:
            try:
                self.req_info = IdpServerSettings_.IDP.parse_authn_request(saml_msg["SAMLRequest"],
                                                                           BINDING_HTTP_REDIRECT)
            except KeyError:
                resp = BadRequest("Message signature verification failure")
                return resp(self.environ, self.start_response)

            if not self.req_info:
                resp = BadRequest("Message parsing failed")
                return resp(self.environ, self.start_response)

            _req = self.req_info.message

            if "SigAlg" in saml_msg and "Signature" in saml_msg:
                # Signed request
                issuer = _req.issuer.text
                _certs = IdpServerSettings_.IDP.metadata.certs(issuer, "any", "signing")
                verified_ok = False
                for cert_name, cert in _certs:
                    if verify_redirect_signature(saml_msg, IdpServerSettings_.IDP.sec.sec_backend, cert):
                        verified_ok = True
                        break
                if not verified_ok:
                    resp = BadRequest("Message signature verification failure")
                    return resp(self.environ, self.start_response)

            if self.user:
                saml_msg["req_info"] = self.req_info
                if _req.force_authn is not None and _req.force_authn.lower() == "true":
                    key = self._store_request(saml_msg)
                    return self.not_authn(key, _req.requested_authn_context)
                else:
                    return self.operation(saml_msg, BINDING_HTTP_REDIRECT)
            else:
                saml_msg["req_info"] = self.req_info
                key = self._store_request(saml_msg)
                return self.not_authn(key, _req.requested_authn_context)
        else:
            return self.operation(saml_msg, BINDING_HTTP_REDIRECT)

    def post(self):
        """
        The HTTP-Post endpoint
        """
        logger.info("--- In SSO POST ---")
        saml_msg = self.unpack_either()

        try:
            _key = saml_msg["key"]
            saml_msg = IdpServerSettings_.IDP.ticket[_key]
            self.req_info = saml_msg["req_info"]
            del IdpServerSettings_.IDP.ticket[_key]
        except KeyError:
            self.req_info = IdpServerSettings_.IDP.parse_authn_request(saml_msg["SAMLRequest"], BINDING_HTTP_POST)
            _req = self.req_info.message
            if self.user:
                if _req.force_authn is not None and _req.force_authn.lower() == "true":
                    saml_msg["req_info"] = self.req_info
                    key = self._store_request(saml_msg)
                    return self.not_authn(key, _req.requested_authn_context)
                else:
                    return self.operation(saml_msg, BINDING_HTTP_POST)
            else:
                saml_msg["req_info"] = self.req_info
                key = self._store_request(saml_msg)
                return self.not_authn(key, _req.requested_authn_context)
        else:
            return self.operation(saml_msg, BINDING_HTTP_POST)

    # def artifact(self):
    # # Can be either by HTTP_Redirect or HTTP_POST
    #     _req = self._store_request(self.unpack_either())
    #     if isinstance(_req, basestring):
    #         return self.not_authn(_req)
    #     return self.artifact_operation(_req)

    def ecp(self):
        # The ECP interface
        logger.info("--- ECP SSO ---")
        resp = None

        try:
            authz_info = self.environ["HTTP_AUTHORIZATION"]
            if authz_info.startswith("Basic "):
                try:
                    _info = base64.b64decode(authz_info[6:])
                except TypeError:
                    resp = Unauthorized()
                else:
                    try:
                        (user, passwd) = _info.split(":")
                        if is_equal(PASSWD[user], passwd):
                            resp = Unauthorized()
                        self.user = user
                        self.environ["idp.authn"] = IdpServerSettings_.AUTHN_BROKER.get_authn_by_accr(PASSWORD)
                    except ValueError:
                        resp = Unauthorized()
            else:
                resp = Unauthorized()
        except KeyError:
            resp = Unauthorized()

        if resp:
            return resp(self.environ, self.start_response)

        _dict = self.unpack_soap()
        self.response_bindings = [BINDING_PAOS]
        # Basic auth ?!
        self.op_type = "ecp"
        return self.operation(_dict, BINDING_SOAP)


# -----------------------------------------------------------------------------
# === Authentication ====
# -----------------------------------------------------------------------------


def do_authentication(environ, start_response, authn_context, key, redirect_uri, headers=None):
    """
    Display the login form
    """
    logger.debug("Do authentication")
    auth_info = IdpServerSettings_.AUTHN_BROKER.pick(authn_context)

    if len(auth_info):
        method, reference = auth_info[0]
        logger.debug("Authn chosen: %s (ref=%s)", method, reference)
        return method(environ, start_response, reference, key, redirect_uri, headers)
    else:
        resp = Unauthorized("No usable authentication method")
        return resp(environ, start_response)


# -----------------------------------------------------------------------------


PASSWD = {
    "testuser": "qwerty",
    "roland": "dianakra",
    "babs": "howes",
    "upper": "crust",
    "testuser2": "abcd1234",
    "testuser3": "ABCD1234",
}


def username_password_authn(environ, start_response, reference, key, redirect_uri, headers=None):
    """
    Display the login form
    """
    logger.info("The login page")

    kwargs = dict(mako_template="login.mako", template_lookup=LOOKUP)
    if headers:
        kwargs["headers"] = headers

    resp = Response(**kwargs)

    argv = {
        "action": "/verify",
        "login": "",
        "password": "",
        "key": key,
        "authn_reference": reference,
        "redirect_uri": redirect_uri,
    }
    logger.info("do_authentication argv: %s", argv)
    return resp(environ, start_response, **argv)


def verify_username_and_password(dic):
    # verify username and password
    username = dic["login"][0]
    password = dic["password"][0]
    if PASSWD[username] == password:
        return True, username
    else:
        return False, None


def do_verify(environ, start_response, _):
    query_str = get_post(environ)
    if not isinstance(query_str, str):
        query_str = query_str.decode("ascii")
    query = parse_qs(query_str)

    logger.debug("do_verify: %s", query)

    try:
        _ok, user = verify_username_and_password(query)
    except KeyError:
        _ok = False
        user = None

    if not _ok:
        resp = Unauthorized("Unknown user or wrong password")
    else:
        uid = rndstr(24)
        IdpServerSettings_.IDP.cache.uid2user[uid] = user
        IdpServerSettings_.IDP.cache.user2uid[user] = uid
        logger.debug("Register %s under '%s'", user, uid)

        kaka = set_cookie("idpauthn", "/", uid, query["authn_reference"][0])

        lox = f"{query['redirect_uri'][0]}?id={uid}&key={query['key'][0]}"
        logger.debug("Redirect => %s", lox)
        resp = Redirect(lox, headers=[kaka], content="text/html")

    return resp(environ, start_response)


def not_found(environ, start_response):
    """Called if no URL matches."""
    resp = NotFound()
    return resp(environ, start_response)


# -----------------------------------------------------------------------------
# === Single log out ===
# -----------------------------------------------------------------------------


# def _subject_sp_info(req_info):
#    # look for the subject
#    subject = req_info.subject_id()
#    subject = subject.text.strip()
#    sp_entity_id = req_info.message.issuer.text.strip()
#    return subject, sp_entity_id


class SLO(Service):
    def do(self, request, binding, relay_state="", encrypt_cert=None, **kwargs):

        logger.info("--- Single Log Out Service ---")
        try:
            logger.debug("req: '%s'", request)
            req_info = IdpServerSettings_.IDP.parse_logout_request(request, binding)
        except Exception as exc:
            logger.error("Bad request: %s", exc)
            resp = BadRequest(f"{exc}")
            return resp(self.environ, self.start_response)

        msg = req_info.message
        if msg.name_id:
            lid = IdpServerSettings_.IDP.ident.find_local_id(msg.name_id)
            logger.info("local identifier: %s", lid)
            if lid in IdpServerSettings_.IDP.cache.user2uid:
                uid = IdpServerSettings_.IDP.cache.user2uid[lid]
                if uid in IdpServerSettings_.IDP.cache.uid2user:
                    del IdpServerSettings_.IDP.cache.uid2user[uid]
                del IdpServerSettings_.IDP.cache.user2uid[lid]
            # remove the authentication
            try:
                IdpServerSettings_.IDP.session_db.remove_authn_statements(msg.name_id)
            except KeyError as exc:
                logger.error("Unknown session: %s", exc)
                resp = ServiceError("Unknown session: %s", exc)
                return resp(self.environ, self.start_response)

        resp = IdpServerSettings_.IDP.create_logout_response(msg, [binding])

        if binding == BINDING_SOAP:
            destination = ""
            response = False
        else:
            binding, destination = IdpServerSettings_.IDP.pick_binding("single_logout_service",
                                                                       [binding], "spsso", req_info)
            response = True

        try:
            hinfo = IdpServerSettings_.IDP.apply_binding(binding, f"{resp}",
                                                         destination, relay_state, response=response)
        except Exception as exc:
            logger.error("ServiceError: %s", exc)
            resp = ServiceError(f"{exc}")
            return resp(self.environ, self.start_response)

        # _tlh = dict2list_of_tuples(hinfo["headers"])
        delco = delete_cookie(self.environ, "idpauthn")
        if delco:
            hinfo["headers"].append(delco)
        logger.info("Header: %s", (hinfo["headers"],))

        if binding == BINDING_HTTP_REDIRECT:
            for key, value in hinfo["headers"]:
                if key.lower() == "location":
                    resp = Redirect(value, headers=hinfo["headers"])
                    return resp(self.environ, self.start_response)

            resp = ServiceError("missing Location header")
            return resp(self.environ, self.start_response)
        else:
            resp = Response(hinfo["data"], headers=hinfo["headers"])
            return resp(self.environ, self.start_response)


# ----------------------------------------------------------------------------
# Manage Name ID service
# ----------------------------------------------------------------------------


class NMI(Service):
    def do(self, query, binding, relay_state="", encrypt_cert=None):
        logger.info("--- Manage Name ID Service ---")
        req = IdpServerSettings_.IDP.parse_manage_name_id_request(query, binding)
        request = req.message

        # Do the necessary stuff
        name_id = IdpServerSettings_.IDP.ident.handle_manage_name_id_request(
            request.name_id, request.new_id, request.new_encrypted_id, request.terminate
        )

        logger.debug("New NameID: %s", name_id)

        _resp = IdpServerSettings_.IDP.create_manage_name_id_response(request)

        # It's using SOAP binding
        hinfo = IdpServerSettings_.IDP.apply_binding(BINDING_SOAP, f"{_resp}", "", relay_state, response=True)

        resp = Response(hinfo["data"], headers=hinfo["headers"])
        return resp(self.environ, self.start_response)


# ----------------------------------------------------------------------------
# === Assertion ID request ===
# ----------------------------------------------------------------------------


# Only URI binding
class AIDR(Service):
    def do(self, aid, binding, relay_state="", encrypt_cert=None):
        logger.info("--- Assertion ID Service ---")

        try:
            assertion = IdpServerSettings_.IDP.create_assertion_id_request_response(aid)
        except Unknown:
            resp = NotFound(aid)
            return resp(self.environ, self.start_response)

        hinfo = IdpServerSettings_.IDP.apply_binding(BINDING_URI, f"{assertion}", response=True)

        logger.debug("HINFO: %s", hinfo)
        resp = Response(hinfo["data"], headers=hinfo["headers"])
        return resp(self.environ, self.start_response)

    def operation(self, _dict, binding, **kwargs):
        logger.debug("_operation: %s", _dict)
        if not _dict or "ID" not in _dict:
            resp = BadRequest("Error parsing request or no request")
            return resp(self.environ, self.start_response)

        return self.do(_dict["ID"], binding, **kwargs)


# ----------------------------------------------------------------------------
# === Artifact resolve service ===
# ----------------------------------------------------------------------------


class ARS(Service):
    def do(self, request, binding, relay_state="", encrypt_cert=None):
        _req = IdpServerSettings_.IDP.parse_artifact_resolve(request, binding)

        msg = IdpServerSettings_.IDP.create_artifact_response(_req, _req.artifact.text)

        hinfo = IdpServerSettings_.IDP.apply_binding(BINDING_SOAP, f"{msg}", "", "", response=True)

        resp = Response(hinfo["data"], headers=hinfo["headers"])
        return resp(self.environ, self.start_response)


# ----------------------------------------------------------------------------
# === Authn query service ===
# ----------------------------------------------------------------------------


# Only SOAP binding
class AQS(Service):
    def do(self, request, binding, relay_state="", encrypt_cert=None):
        logger.info("--- Authn Query Service ---")
        _req = IdpServerSettings_.IDP.parse_authn_query(request, binding)
        _query = _req.message

        msg = IdpServerSettings_.IDP.create_authn_query_response(_query.subject,
                                                                 _query.requested_authn_context, _query.session_index)

        logger.debug("response: %s", msg)
        hinfo = IdpServerSettings_.IDP.apply_binding(BINDING_SOAP, f"{msg}", "", "", response=True)

        resp = Response(hinfo["data"], headers=hinfo["headers"])
        return resp(self.environ, self.start_response)


# ----------------------------------------------------------------------------
# === Attribute query service ===
# ----------------------------------------------------------------------------


# Only SOAP binding
class ATTR(Service):
    def do(self, request, binding, relay_state="", encrypt_cert=None):
        logger.info("--- Attribute Query Service ---")

        _req = IdpServerSettings_.IDP.parse_attribute_query(request, binding)
        _query = _req.message

        name_id = _query.subject.name_id
        uid = name_id.text
        logger.debug("Local uid: %s", uid)
        identity = EXTRA[uid]

        # Comes in over SOAP so only need to construct the response
        args = IdpServerSettings_.IDP.response_args(_query, [BINDING_SOAP])
        msg = IdpServerSettings_.IDP.create_attribute_response(identity, name_id=name_id, **args)

        logger.debug("response: %s", msg)
        hinfo = IdpServerSettings_.IDP.apply_binding(BINDING_SOAP, f"{msg}", "", "", response=True)

        resp = Response(hinfo["data"], headers=hinfo["headers"])
        return resp(self.environ, self.start_response)


# ----------------------------------------------------------------------------
# Name ID Mapping service
# When an entity that shares an identifier for a principal with an identity
# provider wishes to obtain a name identifier for the same principal in a
# particular format or federation namespace, it can send a request to
# the identity provider using this protocol.
# ----------------------------------------------------------------------------


class NIM(Service):
    def do(self, query, binding, relay_state="", encrypt_cert=None):
        req = IdpServerSettings_.IDP.parse_name_id_mapping_request(query, binding)
        request = req.message
        # Do the necessary stuff
        try:
            name_id = IdpServerSettings_.IDP.ident.handle_name_id_mapping_request(
                request.name_id, request.name_id_policy)
        except Unknown:
            resp = BadRequest("Unknown entity")
            return resp(self.environ, self.start_response)
        except PolicyError:
            resp = BadRequest("Unknown entity")
            return resp(self.environ, self.start_response)

        info = IdpServerSettings_.IDP.response_args(request)
        _resp = IdpServerSettings_.IDP.create_name_id_mapping_response(name_id, **info)

        # Only SOAP
        hinfo = IdpServerSettings_.IDP.apply_binding(BINDING_SOAP, f"{_resp}", "", "", response=True)

        resp = Response(hinfo["data"], headers=hinfo["headers"])
        return resp(self.environ, self.start_response)


# ----------------------------------------------------------------------------
# Cookie handling
# ----------------------------------------------------------------------------


def info_from_cookie(kaka):
    logger.debug("KAKA: %s", kaka)
    if kaka:
        cookie_obj = SimpleCookie(kaka)
        morsel = cookie_obj.get("idpauthn", None)
        if morsel:
            try:
                data = base64.b64decode(morsel.value)
                if not isinstance(data, str):
                    data = data.decode("ascii")
                key, ref = data.split(":", 1)
                return IdpServerSettings_.IDP.cache.uid2user[key], ref
            except (KeyError, TypeError):
                return None, None
        else:
            logger.debug("No idpauthn cookie")
    return None, None


def delete_cookie(environ, name):
    kaka = environ.get("HTTP_COOKIE", "")
    logger.debug("delete KAKA: %s", kaka)
    if kaka:
        cookie_obj = SimpleCookie(kaka)
        morsel = cookie_obj.get(name, None)
        cookie = SimpleCookie()
        cookie[name] = ""
        cookie[name]["path"] = "/"
        logger.debug("Expire: %s", morsel)
        cookie[name]["expires"] = _expiration("dawn")
        return tuple(cookie.output().split(": ", 1))
    return None


def set_cookie(name, _, *args):
    cookie = SimpleCookie()

    data = ":".join(args)
    if not isinstance(data, bytes):
        data = data.encode("ascii")

    data64 = base64.b64encode(data)
    if not isinstance(data64, str):
        data64 = data64.decode("ascii")

    cookie[name] = data64
    cookie[name]["path"] = "/"
    cookie[name]["expires"] = _expiration(5)  # 5 minutes from now
    logger.debug("Cookie expires: %s", cookie[name]["expires"])
    return tuple(cookie.output().split(": ", 1))


# ----------------------------------------------------------------------------


# map urls to functions
AUTHN_URLS = [
    # sso
    (r"sso/post$", (SSO, "post")),
    (r"sso/post/(.*)$", (SSO, "post")),
    (r"sso/redirect$", (SSO, "redirect")),
    (r"sso/redirect/(.*)$", (SSO, "redirect")),
    (r"sso/art$", (SSO, "artifact")),
    (r"sso/art/(.*)$", (SSO, "artifact")),
    # slo
    (r"slo/redirect$", (SLO, "redirect")),
    (r"slo/redirect/(.*)$", (SLO, "redirect")),
    (r"slo/post$", (SLO, "post")),
    (r"slo/post/(.*)$", (SLO, "post")),
    (r"slo/soap$", (SLO, "soap")),
    (r"slo/soap/(.*)$", (SLO, "soap")),
    #
    (r"airs$", (AIDR, "uri")),
    (r"ars$", (ARS, "soap")),
    # mni
    (r"mni/post$", (NMI, "post")),
    (r"mni/post/(.*)$", (NMI, "post")),
    (r"mni/redirect$", (NMI, "redirect")),
    (r"mni/redirect/(.*)$", (NMI, "redirect")),
    (r"mni/art$", (NMI, "artifact")),
    (r"mni/art/(.*)$", (NMI, "artifact")),
    (r"mni/soap$", (NMI, "soap")),
    (r"mni/soap/(.*)$", (NMI, "soap")),
    # nim
    (r"nim$", (NIM, "soap")),
    (r"nim/(.*)$", (NIM, "soap")),
    #
    (r"aqs$", (AQS, "soap")),
    (r"attr$", (ATTR, "soap")),
]

NON_AUTHN_URLS = [
    # (r'login?(.*)$', do_authentication),
    (r"verify?(.*)$", do_verify),
    (r"sso/ecp$", (SSO, "ecp")),
]


# ----------------------------------------------------------------------------


def metadata(environ, start_response):
    try:
        path = IdpServerSettings_.args.path[:]
        if path is None or len(path) == 0:
            path = os.path.dirname(os.path.abspath(__file__))
        if path[-1] != "/":
            path += "/"
        metadata = create_metadata_string(
            path + IdpServerSettings_.args.config,
            IdpServerSettings_.IDP.config,
            IdpServerSettings_.args.valid,
            IdpServerSettings_.args.cert,
            IdpServerSettings_.args.keyfile,
            IdpServerSettings_.args.id,
            IdpServerSettings_.args.name,
            IdpServerSettings_.args.sign,
        )
        start_response("200 OK", [("Content-Type", "text/xml")])
        return [metadata]
    except Exception as ex:
        logger.error("An error occured while creating metadata: %s", ex.message)
        return not_found(environ, start_response)


def staticfile(environ, start_response):
    try:
        path = IdpServerSettings_.args.path[:]
        if path is None or len(path) == 0:
            path = os.path.dirname(os.path.abspath(__file__))
        if path[-1] != "/":
            path += "/"
        path += environ.get("PATH_INFO", "").lstrip("/")
        path = os.path.realpath(path)
        if not path.startswith(IdpServerSettings_.args.path):
            resp = Unauthorized()
            return resp(environ, start_response)
        start_response("200 OK", [("Content-Type", "text/xml")])
        return open(path).read()
    except Exception as ex:
        logger.error("An error occured while creating metadata: %s", ex.message)
        return not_found(environ, start_response)


def application(environ, start_response):
    """
    The main WSGI application. Dispatch the current request to
    the functions from above and store the regular expression
    captures in the WSGI environment as  `myapp.url_args` so that
    the functions from above can access the url placeholders.
    If nothing matches, call the `not_found` function.
    :param environ: The HTTP application environment
    :param start_response: The application to run when the handling of the
        request is done
    :return: The response as a list of lines
    """

    path = environ.get("PATH_INFO", "").lstrip("/")

    if path == "idp.xml":
        return metadata(environ, start_response)

    kaka = environ.get("HTTP_COOKIE", None)
    logger.info("<application> PATH: %s", path)

    if kaka:
        logger.info("= KAKA =")
        user, authn_ref = info_from_cookie(kaka)
        if authn_ref:
            environ["idp.authn"] = IdpServerSettings_.AUTHN_BROKER[authn_ref]
    else:
        try:
            query = parse_qs(environ["QUERY_STRING"])
            logger.debug("QUERY: %s", query)
            user = IdpServerSettings_.IDP.cache.uid2user[query["id"][0]]
        except KeyError:
            user = None

    url_patterns = AUTHN_URLS
    if not user:
        logger.info("-- No USER --")
        # insert NON_AUTHN_URLS first in case there is no user
        url_patterns = NON_AUTHN_URLS + url_patterns

    for regex, callback in url_patterns:
        match = re.search(regex, path)
        if match is not None:
            try:
                environ["myapp.url_args"] = match.groups()[0]
            except IndexError:
                environ["myapp.url_args"] = path

            logger.debug("Callback: %s", callback)
            if isinstance(callback, tuple):
                cls = callback[0](environ, start_response, user)
                func = getattr(cls, callback[1])

                return func()
            return callback(environ, start_response, user)

    if re.search(r"static/.*", path) is not None:
        return staticfile(environ, start_response)
    return not_found(environ, start_response)


# ----------------------------------------------------------------------------

class IdpServerSettings:
    def __init__(self):
        self.AUTHN_BROKER = AuthnBroker()
        self.IDP = None
        self.args = None


IdpServerSettings_ = IdpServerSettings()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", dest="path", help="Path to configuration file.",
                        default="./idp_conf.py")
    parser.add_argument(
        "-v",
        dest="valid",
        help="How long, in days, the metadata is valid from " "the time of creation",
    )
    parser.add_argument("-c", dest="cert", help="certificate")
    parser.add_argument("-i", dest="id", help="The ID of the entities descriptor")
    parser.add_argument("-k", dest="keyfile", help="A file with a key to sign the metadata with")
    parser.add_argument("-n", dest="name")
    parser.add_argument("-s", dest="sign", action="store_true", help="sign the metadata")
    parser.add_argument("-m", dest="mako_root", default="./")
    parser.add_argument("-config", dest="config", default="idp_conf", help="configuration file")

    IdpServerSettings_.args = parser.parse_args()

    try:
        CONFIG = importlib.import_module(IdpServerSettings_.args.config)
    except ImportError as e:
        logger.error("Idp_conf cannot be imported : %s, Trying by setting the system path...", e)
        sys.path.append(os.path.join(DOCS_SERVER_PATH))
        CONFIG = importlib.import_module(IdpServerSettings_.args.config)

    IdpServerSettings_.AUTHN_BROKER.add(authn_context_class_ref(PASSWORD), username_password_authn, 10, CONFIG.BASE)
    IdpServerSettings_.AUTHN_BROKER.add(authn_context_class_ref(UNSPECIFIED), "", 0, CONFIG.BASE)

    IdpServerSettings_.IDP = server.Server(IdpServerSettings_.args.config, cache=Cache())
    IdpServerSettings_.IDP.ticket = {}

    HOST = CONFIG.HOST
    PORT = CONFIG.PORT

    sign_alg = None
    digest_alg = None
    try:
        sign_alg = CONFIG.SIGN_ALG
    except AttributeError:
        pass
    try:
        digest_alg = CONFIG.DIGEST_ALG
    except AttributeError:
        pass
    ds.DefaultSignature(sign_alg, digest_alg)

    ssl_context = None
    _https = ""
    if CONFIG.HTTPS:
        _https = "using HTTPS"
        # Creating an SSL context
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
        ssl_context.load_cert_chain(CONFIG.SERVER_CERT, CONFIG.SERVER_KEY)
    SRV = WSGIServer(HOST, PORT, application, ssl_context=ssl_context)

    logger.info("Server starting")
    print(f"IDP listening on {HOST}:{PORT}{_https}")
    try:
        SRV.start()
    except KeyboardInterrupt:
        SRV.stop()


if __name__ == "__main__":
    main()
