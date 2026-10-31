"""在线音源要用的最小密码学件：只实现 AES-128 ECB/CBC（PKCS7）。

为什么不 import cryptography：那个包是 Rust 扩展，打进 onefile 后体积和常驻内存
都翻倍，而这里每次只加解密几百字节到几 KB，纯 Python 完全够快。

S 盒、Rcon 都是运行时按定义算出来的，不手抄常量表，避免抄错一位导致全链路静默失效。
"""
import hashlib

_SBOX = None
_INV_SBOX = None
_RCON = None

def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11b
    return a & 0xff

def _mul(a, b):
    """GF(2^8) 乘法。"""
    out = 0
    for _ in range(8):
        if b & 1:
            out ^= a
        b >>= 1
        a = _xtime(a)
    return out

def _inv(a):
    """GF(2^8) 逆元：a^254（平方乘做，比暴力查表快两个数量级）。"""
    if a == 0:
        return 0
    result = 1
    base = a
    power = 254
    while power:
        if power & 1:
            result = _mul(result, base)
        base = _mul(base, base)
        power >>= 1
    return result

def _rotl8(v, n):
    return ((v << n) | (v >> (8 - n))) & 0xff

def _tables():
    global _SBOX, _INV_SBOX, _RCON
    if _SBOX is not None:
        return _SBOX, _INV_SBOX, _RCON
    sbox = []
    for i in range(256):
        inv = _inv(i)
        s = inv
        for shift in (1, 2, 3, 4):
            s ^= _rotl8(inv, shift)
        s ^= 0x63
        sbox.append(s & 0xff)
    inv_sbox = [0] * 256
    for i, v in enumerate(sbox):
        inv_sbox[v] = i
    rcon = [1]
    for i in range(1, 11):
        rcon.append(_xtime(rcon[-1]))
    _SBOX, _INV_SBOX, _RCON = tuple(sbox), tuple(inv_sbox), tuple(rcon)
    return _SBOX, _INV_SBOX, _RCON

def _expand_key(key):
    """16 字节密钥 → 11 轮 44 个字。"""
    sbox, _, rcon = _tables()
    nk = len(key) // 4
    if nk != 4:
        raise ValueError("只支持 AES-128")
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 44):
        t = list(words[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [sbox[b] for b in t]
            t[0] ^= rcon[i // nk - 1]
        words.append([words[i - nk][j] ^ t[j] for j in range(4)])
    return words

def _add_round_key(state, words, rnd):
    for c in range(4):
        w = words[rnd * 4 + c]
        for r in range(4):
            state[r][c] ^= w[r]

def _sub_bytes(state, table):
    for r in range(4):
        for c in range(4):
            state[r][c] = table[state[r][c]]

def _shift_rows(state, back=False):
    for r in range(1, 4):
        if back:
            state[r] = state[r][-r:] + state[r][:-r]
        else:
            state[r] = state[r][r:] + state[r][:r]

def _mix_columns(state, back=False):
    for c in range(4):
        a = [state[r][c] for r in range(4)]
        if not back:
            b0 = _mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3]
            b1 = a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3]
            b2 = a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3)
            b3 = _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)
        else:
            b0 = _mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9)
            b1 = _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13)
            b2 = _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11)
            b3 = _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)
        for r, v in enumerate((b0, b1, b2, b3)):
            state[r][c] = v

def _encrypt_block(block, words):
    state = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
    _add_round_key(state, words, 0)
    for rnd in range(1, 10):
        _sub_bytes(state, _SBOX)
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, words, rnd)
    _sub_bytes(state, _SBOX)
    _shift_rows(state)
    _add_round_key(state, words, 10)
    out = bytearray(16)
    for c in range(4):
        for r in range(4):
            out[r + 4 * c] = state[r][c]
    return bytes(out)

def _decrypt_block(block, words):
    state = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
    _add_round_key(state, words, 10)
    for rnd in range(9, 0, -1):
        _shift_rows(state, back=True)
        _sub_bytes(state, _INV_SBOX)
        _add_round_key(state, words, rnd)
        _mix_columns(state, back=True)
    _shift_rows(state, back=True)
    _sub_bytes(state, _INV_SBOX)
    _add_round_key(state, words, 0)
    out = bytearray(16)
    for c in range(4):
        for r in range(4):
            out[r + 4 * c] = state[r][c]
    return bytes(out)

def pkcs7_pad(data, size=16):
    n = size - (len(data) % size)
    return data + bytes([n]) * n

def pkcs7_unpad(data):
    if not data:
        return data
    n = data[-1]
    if n < 1 or n > 16 or len(data) < n:
        return data
    if data[-n:] != bytes([n]) * n:
        return data
    return data[:-n]

def aes_ecb_encrypt(data, key):
    """PKCS7 填充后按块加密，返回字节。"""
    words = _expand_key(key)
    data = pkcs7_pad(data)
    out = bytearray()
    for i in range(0, len(data), 16):
        out += _encrypt_block(data[i:i + 16], words)
    return bytes(out)

def aes_ecb_decrypt(data, key):
    words = _expand_key(key)
    out = bytearray()
    for i in range(0, len(data) - 15, 16):
        out += _decrypt_block(data[i:i + 16], words)
    return pkcs7_unpad(bytes(out))

def aes_cbc_encrypt(data, key, iv):
    words = _expand_key(key)
    data = pkcs7_pad(data)
    prev = iv
    out = bytearray()
    for i in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
        cur = _encrypt_block(block, words)
        out += cur
        prev = cur
    return bytes(out)

def aes_cbc_decrypt(data, key, iv):
    words = _expand_key(key)
    prev = iv
    out = bytearray()
    for i in range(0, len(data) - 15, 16):
        cur = data[i:i + 16]
        dec = _decrypt_block(cur, words)
        out += bytes(a ^ b for a, b in zip(dec, prev))
        prev = cur
    return pkcs7_unpad(bytes(out))

def md5(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def md5_upper(text):
    return md5(text).upper()

def self_test():
    """FIPS-197 附录 C.1 的官方向量，验 S 盒/密钥扩展/轮函数都没写错。"""
    key = bytes(range(16))
    plain = bytes([0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77,
                   0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff])
    want = "69c4e0d86a7b0430d8cdb78070b4c55a"
    words = _expand_key(key)
    got = _encrypt_block(plain, words).hex()
    if got != want:
        raise AssertionError("AES 加密向量不符：期望 %s 实得 %s" % (want, got))
    if _decrypt_block(bytes.fromhex(want), words) != plain:
        raise AssertionError("AES 解密向量不符")
    blob = b'{"test":"\xe4\xb8\xad\xe6\x96\x87"}'
    if aes_ecb_decrypt(aes_ecb_encrypt(blob, key), key) != blob:
        raise AssertionError("ECB 往返不一致")
    if aes_cbc_decrypt(aes_cbc_encrypt(blob, key, bytes(16)), key, bytes(16)) != blob:
        raise AssertionError("CBC 往返不一致")
    return want
