"""飞书扫码授权流（RFC 8628 设备授权，依赖仅 httpx）。

二维码指向飞书官方授权页 accounts.feishu.cn，绝不指向自建站（避免被吞 # 哈希 / 要求公网可达）。
成功后返回 PersonalAgent 自建应用的 client_id / client_secret。
"""
import httpx

ENDPOINT = "https://accounts.feishu.cn/oauth/v1/app/registration"
CHANNEL = "lifeos"


class FeishuDeviceFlow:
    def __init__(self, endpoint: str = ENDPOINT, channel: str = CHANNEL):
        self.endpoint = endpoint
        self.channel = channel

    async def start(self) -> dict:
        """发起授权，返回 {scan_url, poll_token, expires_in}。"""
        async with httpx.AsyncClient(timeout=20) as c:
            # 1. init：确认支持 client_secret（飞书要求 form-urlencoded，字段在顶层）
            r = await c.post(self.endpoint, data={"action": "init"})
            if r.status_code != 200:
                return {"status": "error", "reason": f"init HTTP {r.status_code}"}
            methods = (r.json().get("supported_auth_methods") or [])
            if "client_secret" not in methods:
                return {"status": "error", "reason": "不支持 client_secret 授权"}

            # 2. begin：拿到 device_code + verification_uri_complete（form-urlencoded）
            r2 = await c.post(self.endpoint, data={
                "action": "begin",
                "archetype": "PersonalAgent",
                "auth_method": "client_secret",
                "request_user_info": "open_id",
            })
            if r2.status_code != 200:
                return {"status": "error", "reason": f"begin HTTP {r2.status_code}"}
            d = r2.json()
            device_code = d.get("device_code")
            uri = d.get("verification_uri_complete", "")
            if not device_code or not uri:
                return {"status": "error", "reason": "未返回 device_code / 授权链接"}
            scan_url = uri + (f"&source={self.channel}" if "source=" not in uri else "")
            return {"status": "ok", "scan_url": scan_url, "poll_token": device_code,
                    "expires_in": d.get("expires_in", 300)}

    async def poll(self, device_code: str) -> dict:
        """轮询授权结果。飞书在 pending/denied/expired 时返回 4xx，真实状态在 body.error。"""
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(self.endpoint, data={"action": "poll", "device_code": device_code})
            try:
                d = r.json()
            except Exception:
                return {"status": "error", "reason": f"poll HTTP {r.status_code}"}
            err = d.get("error")
            if err in ("authorization_pending", "slow_down"):
                return {"status": "pending"}
            if err == "expired_token":
                return {"status": "expired"}
            if err == "access_denied":
                return {"status": "denied"}
            app_id = d.get("client_id")
            app_secret = d.get("client_secret")
            if app_id and app_secret:
                return {"status": "success", "app_id": app_id, "app_secret": app_secret}
            if err:
                return {"status": "error", "reason": f"飞书返回: {err}"}
            return {"status": "pending"}
