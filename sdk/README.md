# ApprovalCenter Python SDK

[approval_center_sdk.py](approval_center_sdk.py) is a standalone HTTP client for the ApprovalCenter API v1.
Copy the file into an integration project and import it directly

It requires Python 3.9 or later, HTTPX, and Pydantic v2.

It's licensed under GPLv3.

Install the dependencies in the integration project's Python environment:

```bash
python -m pip install httpx 'pydantic>=2,<3'
```

Use `ApprovalCenterClient` for synchronous code or `AsyncApprovalCenterClient` for asynchronous code.
Both use the service base URL, `client_id`, and `client_secret`

Set `CreateApprovalRequest.reference_key` to associate approvals with a business object.
Use the same string in `ApprovalListRequest.reference_key` for exact-match filtering.
Leaving the filter as `None` includes approvals with and without a key.

`cancel_approval` accepts an `ApprovalIdRequest` and returns the complete approval.
Repeating cancellation of a cancelled approval preserves its cancellation time.

HTTP failures with a valid `code` and `message` response raise `ApprovalCenterAPIError`.
The exception exposes `status_code`, `code`, and `message`, and retains the original HTTPX `request` and `response`.
It subclasses `httpx.HTTPStatusError`; other HTTP failures retain that original exception type.
Network errors and Pydantic validation errors propagate unchanged.
