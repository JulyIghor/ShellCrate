#!/usr/bin/env python3
"""Download checksum-pinned build inputs with shared verification and retries."""
import hashlib
import http.client
import json
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DOWNLOAD_ATTEMPTS = 4


class HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != 'https':
            raise ValueError('Pinned download redirects require HTTPS')
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        source = urllib.parse.urlsplit(req.full_url)
        if redirected and (source.hostname, source.port or 443) != (target.hostname, target.port or 443):
            # Registry authorization belongs to GHCR, not its signed CDN URL.
            for name in list(redirected.headers):
                if name.lower() in ('authorization', 'proxy-authorization', 'cookie'):
                    redirected.remove_header(name)
        return redirected


class DownloadResponseError(ValueError):
    """A response that must never enter the verified artifact cache."""


def digest(path):
    with path.open('rb') as stream:
        result = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def request(url, headers=None):
    if not url.startswith('https://'):
        raise ValueError('Pinned downloads require HTTPS')
    opener = urllib.request.build_opener(HTTPSRedirectHandler())
    # The default proxy and TLS handlers remain enabled for Xcode Cloud.
    return opener.open(urllib.request.Request(url, headers=headers or {}), timeout=90)


def download(url, checksum, destination, alternatives=()):
    """Return a verified cached artifact, publishing new bytes only after validation."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Pinned downloads require HTTPS without embedded credentials')
    if not re.fullmatch(r'[0-9a-f]{64}', checksum):
        raise ValueError('Unpinned artifact download')
    destination = Path(destination)
    for candidate in (destination, *alternatives):
        if candidate.is_file() and digest(candidate) == checksum:
            return candidate
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Always request the original archive bytes. Never disable the Cloud proxy
    # or TLS verification, and never accept a different digest after a retry.
    headers = {'Accept-Encoding': 'identity', 'Cache-Control': 'no-cache, no-transform', 'Pragma': 'no-cache'}
    label = parsed.hostname + parsed.path  # Exclude signed URL queries and credentials from logs.
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            # Cloud's Python transport can repeatedly end a response early.
            # Retry through the system transport with a fresh full request.
            download_attempt(url, checksum, destination, dict(headers), native=attempt > 1)
            return destination
        except (DownloadResponseError, urllib.error.URLError, http.client.HTTPException,
                TimeoutError, socket.timeout, ConnectionError, ssl.SSLError, json.JSONDecodeError) as error:
            if isinstance(error, DownloadResponseError):
                reason = str(error)
            elif isinstance(error, urllib.error.HTTPError):
                reason = 'HTTP ' + str(error.code)
                if error.fp is not None:
                    error.close()
            elif isinstance(error, urllib.error.URLError):
                reason = 'network request failed (' + type(error.reason).__name__ + ')'
            else:
                reason = type(error).__name__
            print(f'Download attempt {attempt}/{DOWNLOAD_ATTEMPTS} failed for {label} - {reason}',
                  file=sys.stderr, flush=True)
            permanent = isinstance(error, urllib.error.HTTPError) and error.code < 500 and error.code not in (401, 403, 408, 429)
            if attempt == DOWNLOAD_ATTEMPTS or permanent:
                raise ValueError(f'Cannot download verified input {label} - {reason}') from None
            time.sleep(2 ** (attempt - 1))


def native_transfer(url, headers, destination):
    """Use macOS curl without exposing credentials in arguments or diagnostics."""
    def quoted(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\r', '\\r').replace('\n', '\\n') + '"'
    configuration = ['url = ' + quoted(url)]
    configuration += ['header = ' + quoted(name + ': ' + value) for name, value in headers.items()]
    proxies = urllib.request.getproxies()
    # Preserve environment and macOS proxy discovery used by urllib.
    if proxies.get('https'):
        configuration.append('proxy = ' + quoted(proxies['https']))
    if proxies.get('no'):
        configuration.append('noproxy = ' + quoted(proxies['no']))
    try:
        result = subprocess.run([
            '/usr/bin/curl', '-q', '--config', '-', '--globoff', '--fail', '--location',
            '--proto', '=https', '--proto-redir', '=https', '--max-redirs', '10',
            '--connect-timeout', '30', '--max-time', '300', '--silent', '--show-error',
            '--output', str(destination), '--write-out', '{"transfer":%{json},"headers":%{header_json}}'
        ], input='\n'.join(configuration) + '\n', text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=315)
    except subprocess.TimeoutExpired:
        raise DownloadResponseError('Native HTTPS transport timed out') from None
    # stderr and metadata may contain signed redirect URLs. Never log either.
    try:
        metadata = json.loads(result.stdout)
        transfer = metadata['transfer']
        status = int(transfer['response_code'])
        final_url = transfer['url_effective']
        response_headers = {key.lower(): ', '.join(values) for key, values in metadata['headers'].items()}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise DownloadResponseError('Invalid native HTTPS response metadata') from None
    if result.returncode == 22 and status >= 400:
        raise urllib.error.HTTPError(url, status, 'Download failed', {}, None)
    if result.returncode:
        raise DownloadResponseError('Native HTTPS transport failed with curl exit ' + str(result.returncode))
    if not isinstance(final_url, str) or urllib.parse.urlsplit(final_url).scheme != 'https':
        raise DownloadResponseError('Native download redirects require HTTPS')
    return status, response_headers, final_url


def response_details(status, headers, final_url):
    headers = {key.lower(): value for key, value in headers.items()}
    content_length = headers.get('content-length')
    encoding = headers.get('content-encoding', 'identity').lower().strip()
    content_type = headers.get('content-type', 'unknown')
    host = urllib.parse.urlsplit(final_url).hostname
    details = ' '.join(f'HTTP {status}, host {host}, type {content_type}, encoding {encoding}'.split())[:300]
    if status != 200 or headers.get('content-range'):
        raise DownloadResponseError('Expected a complete HTTP 200 response - ' + details)
    if encoding != 'identity':
        raise DownloadResponseError('Unexpected transfer encoding - ' + details)
    if content_length is not None and not re.fullmatch(r'[0-9]{1,20}', content_length):
        raise DownloadResponseError('Invalid Content-Length - ' + details)
    return int(content_length) if content_length is not None else None, details


def download_attempt(url, checksum, destination, headers, native=False):
    # Refresh authorization and follow a new registry redirect on every attempt.
    # An expired CDN URL or an incomplete response must not be resumed or cached.
    if url.startswith('https://ghcr.io/v2/homebrew/core/'):
        repository = url.split('/v2/', 1)[1].split('/blobs/', 1)[0]
        query = urllib.parse.urlencode({'service': 'ghcr.io', 'scope': 'repository:' + repository + ':pull'})
        with request('https://ghcr.io/token?' + query, headers) as response:
            authorization = json.load(response)
        token = authorization.get('token') if isinstance(authorization, dict) else None
        if not isinstance(token, str) or not token or len(token) > 16384 or '\r' in token or '\n' in token:
            raise DownloadResponseError('Invalid registry authorization response')
        headers = dict(headers, Authorization='Bearer ' + token)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            if native:
                expected_size, details = response_details(*native_transfer(url, headers, temporary))
                received, actual = temporary.stat().st_size, digest(temporary)
            else:
                with request(url, headers) as response:
                    expected_size, details = response_details(response.getcode(), response.headers, response.geturl())
                    received = 0
                    checksum_state = hashlib.sha256()
                    for block in iter(lambda: response.read(1024 * 1024), b''):
                        received += len(block)
                        checksum_state.update(block)
                        stream.write(block)
                    actual = checksum_state.hexdigest()
            if actual != checksum or (expected_size is not None and received != expected_size):
                raise DownloadResponseError(
                    f'Checksum or length mismatch - expected SHA256 {checksum}, received SHA256 {actual}, '
                    f'received {received} bytes, Content-Length {expected_size}, {details}')
            stream.close()
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
