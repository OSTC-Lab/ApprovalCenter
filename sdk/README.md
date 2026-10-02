# ApprovalCenter Python SDK

[approval_center_sdk.py](approval_center_sdk.py) 是 ApprovalCenter API v1 的独立客户端文件，
可以直接复制到接入方项目中导入使用，无需安装审批中心的服务端代码。

支持 Python 3.9 及以上版本，使用 GPLv3 许可证。
在接入方的 Python 环境中安装依赖：

```bash
python -m pip install httpx 'pydantic>=2,<3'
```

同步代码使用 `ApprovalCenterClient`，异步代码使用 `AsyncApprovalCenterClient`。
创建客户端时传入服务地址、`client_id` 和 `client_secret`。
客户端应复用，通过 `with` / `async with` 或 `close()` / `aclose()` 释放连接。

每个接口有独立的 Request 和 Response 模型，名称与客户端方法对应。
例如 `get_approval` 使用 `GetApprovalRequest`，返回 `GetApprovalResponse`；
`cancel_approval` 使用 `CancelApprovalRequest`，返回 `CancelApprovalResponse`。

```python
from approval_center_sdk import ApprovalCenterClient, GetApprovalRequest

with ApprovalCenterClient('http://127.0.0.1:8731', 'primebackup', 'secret') as client:
	approval = client.get_approval(GetApprovalRequest(approval_id=1))
```

自定义数据以 `bytes` 传入和返回，SDK 负责 Base64 转换；时间使用整数 Unix 秒。
轮询、重试和业务执行由接入方安排。

HTTP 失败响应能解析出 `code`、`message` 时，抛出 `ApprovalCenterAPIError`。
该异常提供 `status_code`、`code`、`message`，保留原始 HTTPX 请求和响应，
并继承 `httpx.HTTPStatusError`。无法解析的 HTTP 错误、网络异常和 Pydantic 校验异常按原类型抛出。
