"""tests/test_m2.py — M2（auth.py + userinfo.py）单元测试"""

import asyncio
import hashlib
import os
import sys
import types
import urllib.parse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gift_thanks import auth, userinfo  # noqa: E402


# ---------- 1. generate_qr 真实调用 B 站 API ----------

def test_generate_qr_real_api(tmp_path, monkeypatch):
    """真实调用一次 B 站扫码接口，验证能拿到 qrcode_key 和二维码 PNG 路径"""
    monkeypatch.setattr(auth, "get_data_dir", lambda: str(tmp_path))
    qrcode_key, png_path = auth.generate_qr()
    assert isinstance(qrcode_key, str) and len(qrcode_key) > 0
    assert os.path.isfile(png_path)
    # 是有效的 PNG 文件
    with open(png_path, "rb") as f:
        assert f.read(8) == b"\x89PNG\r\n\x1a\n"


# ---------- 2. save/load_cookies 加解密往返 ----------

def test_save_load_cookies_roundtrip(tmp_path, monkeypatch):
    """假 cookie 加密保存后能解密还原"""
    monkeypatch.setattr(auth, "get_data_dir", lambda: str(tmp_path))
    fake = {"SESSDATA": "fake-sessdata-123", "bili_jct": "fake-jct", "DedeUserID": "12345"}
    auth.save_cookies(fake)
    # 落盘文件不是明文（加密生效）
    cred = tmp_path / "cred.bin"
    assert cred.exists()
    assert b"fake-sessdata" not in cred.read_bytes()
    # 往返一致
    assert auth.load_cookies() == fake
    assert auth.get_sessdata() == "fake-sessdata-123"


def test_load_cookies_missing_and_corrupt(tmp_path, monkeypatch):
    """文件不存在返回 None；文件损坏解密失败也返回 None"""
    monkeypatch.setattr(auth, "get_data_dir", lambda: str(tmp_path))
    assert auth.load_cookies() is None
    assert auth.get_sessdata() is None
    (tmp_path / "cred.bin").write_bytes(b"not-a-valid-fernet-token")
    assert auth.load_cookies() is None


# ---------- 3. Wbi 签名纯逻辑 ----------

def test_wbi_mixin_key_known_vector():
    """公开测试向量：img_key+sub_key 混淆后得到固定 mixin_key"""
    img_key = "7cd084941338484aae1ad9425b84077c"
    sub_key = "4932caff0ff746eab6f01bf08b70ac45"
    assert userinfo._get_mixin_key(img_key + sub_key) == "ea1db124af3c7062474693fa704f4ff8"


def test_enc_wbi_signature_format(monkeypatch):
    """固定输入验证签名输出格式：含 wts、w_rid 为 32 位 md5 十六进制且可复算"""
    monkeypatch.setattr(userinfo.time, "time", lambda: 1700000000)
    img_key = "7cd084941338484aae1ad9425b84077c"
    sub_key = "4932caff0ff746eab6f01bf08b70ac45"
    signed = userinfo._enc_wbi({"uids": "1,2,3"}, img_key, sub_key)

    assert str(signed["wts"]) == "1700000000"  # 签名后参数值统一为字符串
    w_rid = signed["w_rid"]
    assert isinstance(w_rid, str) and len(w_rid) == 32
    int(w_rid, 16)  # 必须是合法十六进制

    # 独立复算验证正确性
    mixin_key = "ea1db124af3c7062474693fa704f4ff8"
    query = urllib.parse.urlencode({"uids": "1,2,3", "wts": 1700000000})
    expected = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    assert w_rid == expected


def test_enc_wbi_filters_illegal_chars(monkeypatch):
    """参数值中的 !'()* 会被过滤后再参与签名"""
    monkeypatch.setattr(userinfo.time, "time", lambda: 1700000000)
    signed = userinfo._enc_wbi({"foo": "a!b'c(d)e*f"}, "a" * 32, "b" * 32)
    assert signed["foo"] == "abcdef"  # f 不在过滤字符集内，保留


# ---------- 4. userinfo 批量分组逻辑（mock） ----------

def test_chunk_uids():
    """120 个 uid 应分成 50/50/20 三组"""
    uids = list(range(120))
    chunks = userinfo._chunk_uids(uids)
    assert [len(c) for c in chunks] == [50, 50, 20]
    assert chunks[0] == uids[:50]
    assert chunks[-1] == uids[100:]
    assert userinfo._chunk_uids([]) == []


def test_fill_missing_faces_batches_and_fallback(tmp_path, monkeypatch):
    """mock 数据库与网络层：验证 ≤50/次分组请求、face_path 回填、失败用默认头像"""
    uids = list(range(1, 121))  # 120 个用户
    written = {}                # uid -> face_path
    batch_sizes = []            # 记录每次批量请求的 uid 数
    sleeps = []

    # 假 db 模块（M1 的 db.py 可能尚未合入本分支，故注入 mock）
    fake_db = types.ModuleType("gift_thanks.db")
    fake_db.get_users_missing_face = lambda db_path, session_id: list(uids)
    def _set_face(db_path, uid, face_path):
        written[uid] = face_path
    fake_db.set_user_face_path = _set_face
    monkeypatch.setitem(sys.modules, "gift_thanks.db", fake_db)

    # mock 网络层
    async def fake_wbi_keys(session):
        return ("img", "sub")
    async def fake_fetch(session, keys, batch):
        batch_sizes.append(len(batch))
        # 偶数 uid 返回 face_url，奇数 uid 查不到（走默认头像）
        return {u: f"https://i0.hdslb.com/bfs/face/{u}.jpg" for u in batch if u % 2 == 0}
    async def fake_download(session, url, path):
        # 写入文件模拟下载成功
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"jpg")
        return True
    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(userinfo, "_get_wbi_keys", fake_wbi_keys)
    monkeypatch.setattr(userinfo, "_fetch_user_cards", fake_fetch)
    monkeypatch.setattr(userinfo, "_download_face", fake_download)
    monkeypatch.setattr(userinfo.asyncio, "sleep", fake_sleep)

    db_path = str(tmp_path / "giftthanks.db")
    asyncio.run(userinfo.fill_missing_faces(db_path, 1, "fake-sessdata"))

    # 120 个 uid 分成 ≤50 的批次请求，且批次间有 ≥1 秒间隔
    assert batch_sizes == [50, 50, 20]
    assert all(s >= 1.0 for s in sleeps)

    # 所有 uid 都回填了 face_path
    assert set(written) == set(uids)
    # 偶数 uid 下载成功 → 指向 faces/{uid}.jpg
    faces_dir = os.path.join(str(tmp_path), "faces")
    for u in uids:
        if u % 2 == 0:
            assert written[u] == os.path.join(faces_dir, f"{u}.jpg")
            assert os.path.isfile(written[u])
        else:
            # 查不到 face_url → 用默认占位头像
            assert written[u].endswith("default_avatar.png")


def test_fill_missing_faces_no_missing(tmp_path, monkeypatch):
    """没有缺头像用户时直接返回，不发请求"""
    called = []
    fake_db = types.ModuleType("gift_thanks.db")
    fake_db.get_users_missing_face = lambda db_path, session_id: []
    fake_db.set_user_face_path = lambda *a: called.append(a)
    monkeypatch.setitem(sys.modules, "gift_thanks.db", fake_db)

    async def fail(*a, **k):
        raise AssertionError("不应发起任何网络请求")
    monkeypatch.setattr(userinfo, "_get_wbi_keys", fail)

    asyncio.run(userinfo.fill_missing_faces(str(tmp_path / "g.db"), 1, "s"))
    assert called == []
