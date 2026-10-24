"""
Small sender policy helpers shared by intake and notification paths.
Flow: shared business-flow service called by API routes and workers.
Called by: src/api/rest/routers/document_routers.py,
src/services/email_polling_services.py.
Calls: standard-library or framework helpers.
Tables: no database tables directly.
"""


def is_automated_sender(sender: str | None) -> bool:
    """
    Identify sender addresses that must never receive automatic mail.
    Inputs: sender (str | None).
    Output: bool.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    local = (sender or "").strip().casefold().partition("@")[0]
    return (
        local
        in {
            "no-reply",
            "noreply",
            "do-not-reply",
            "donotreply",
            "mailer-daemon",
            "postmaster",
        }
        or "mailer-daemon" in local
        or "noreply" in local
        or "no-reply" in local
    )


def may_send_automatic_reply(document) -> bool:
    """
    Block auto replies for bulk-tagged and automated intake messages.
    Inputs: document.
    Output: bool.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    if getattr(document, "source_flag", None) in {"bulk_sender", "auto_reply"}:
        return False
    return not is_automated_sender(getattr(document, "email_sender", None))
