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