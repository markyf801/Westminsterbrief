import os
import logging
from flask import render_template

logger = logging.getLogger(__name__)


def send_email(to: str, subject: str, html_body: str, text_body: str) -> bool:
    """Send a transactional email via Postmark.

    When EMAIL_TEST_MODE=true (or POSTMARK_SERVER_TOKEN is unset), logs the
    email to stdout instead of sending — safe for local development.
    """
    token = os.environ.get('POSTMARK_SERVER_TOKEN', '')
    from_addr = os.environ.get('EMAIL_FROM_ADDRESS', 'hello@westminsterbrief.co.uk')
    from_name = os.environ.get('EMAIL_FROM_NAME', 'Westminster Brief')
    test_mode = os.environ.get('EMAIL_TEST_MODE', 'false').lower() == 'true'

    if test_mode or not token:
        print(f'[EMAIL TEST] To={to} | Subject={subject}', flush=True)
        logger.info('[EMAIL TEST] To=%s | Subject=%s', to, subject)
        return True

    try:
        from postmarker.core import PostmarkClient
        client = PostmarkClient(server_token=token)
        client.emails.send(
            From=f'{from_name} <{from_addr}>',
            To=to,
            Subject=subject,
            HtmlBody=html_body,
            TextBody=text_body,
            MessageStream='outbound',
        )
        logger.info('[EMAIL SENT] To=%s | Subject=%s', to, subject)
        return True
    except Exception as e:
        logger.error('[EMAIL FAILED] To=%s | Subject=%s | Error=%s', to, subject, e)
        return False


def send_template_email(to: str, subject: str, template_name: str, **ctx) -> bool:
    """Render Jinja2 email templates and send.

    Looks for templates/emails/<template_name>.html and .txt.
    Must be called within a Flask application context.
    """
    html_body = render_template(f'emails/{template_name}.html', **ctx)
    text_body = render_template(f'emails/{template_name}.txt', **ctx)
    return send_email(to, subject, html_body, text_body)
