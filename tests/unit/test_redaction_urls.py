"""Secrets carried in URLs (webhook verification, OAuth callback) never reach the logs."""
import logging

from agentplatform.core.security.redaction import RedactingFilter, redact


def test_url_secrets_are_redacted():
    line = ('"GET /webhooks/meta?hub.mode=subscribe&hub.challenge=123&hub.verify_token=s3cr3tT0ken'
            '&hub_verify_token=s3cr3tT0ken HTTP/1.1" 200')
    out = redact(line)
    assert "s3cr3tT0ken" not in out and "hub.challenge=123" in out and "hub.mode=subscribe" in out
    assert "abc123" not in redact("/console/channels/instagram/callback?code=abc123&state=xyz")


def test_access_logger_is_filtered():
    from agentplatform.core.security import redaction
    redaction.install()
    assert any(isinstance(f, RedactingFilter) for f in logging.getLogger("uvicorn.access").filters)


def test_access_log_args_keep_their_shape():
    """uvicorn's AccessFormatter unpacks 5 args; the filter must redact them without collapsing the tuple."""
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("203.0.113.4:5", "GET", "/webhooks/meta?hub.verify_token=s3cr3t&hub.challenge=9", "1.1", 200), None)
    assert RedactingFilter().filter(rec)
    client, method, path, version, status = rec.args
    assert "s3cr3t" not in path and "hub.challenge=9" in path and status == 200
    assert "s3cr3t" not in rec.getMessage()


def test_httpx_request_log_hides_app_secret_and_tokens():
    """httpx logs the request URL as an httpx.URL object (not str), on its own logger, through handlers that may be
    created after install() (the worker CLI configures logging after importing the app)."""
    import io

    import httpx

    from agentplatform.core.security import redaction
    redaction.install()
    out = io.StringIO()
    handler = logging.StreamHandler(out)                   # added after install(), no filter of its own
    log = logging.getLogger("httpx")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        url = httpx.URL("https://graph.facebook.com/v26.0/oauth/access_token?grant_type=fb_exchange_token&client_id=1"
                        "&client_secret=0123456789abcdef0123456789abcdef&fb_exchange_token=EAAshortlivedtokenvalue123")
        log.info('HTTP Request: %s %s "%s %d %s"', "GET", url, "HTTP/1.1", 200, "OK")
        try:
            raise httpx.HTTPError(f"Client error for url '{url}'")
        except httpx.HTTPError:
            log.exception("graph call failed")
    finally:
        log.removeHandler(handler)
    text = out.getvalue()
    assert "0123456789abcdef" not in text and "EAAshort" not in text
    assert "client_id=1" in text and "client_secret=[REDACTED]" in text and "HTTP/1.1 200 OK" in text
