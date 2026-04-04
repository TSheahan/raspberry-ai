# Deepgram Live WebSocket 400 Investigation

**SDK:** `deepgram-sdk==6.1.1`  
**Error:** `HTTP/1.1 400 Bad Request` · `dg-error: Invalid query string.`  
**Captured URL:**  
```
GET /v1/listen?channels=1&encoding=linear16&endpointing=300&interim_results=True&language=en-US&model=nova-3&sample_rate=16000&smart_format=True
```

---

## 1. Finding — Root Cause

### Primary cause: Python `bool` serialises to `"True"`, API requires `"true"`

The SDK's `connect()` method in `src/deepgram/listen/v1/client.py`
([source @ 74f48c51](https://raw.githubusercontent.com/deepgram/deepgram-python-sdk/74f48c51/src/deepgram/listen/v1/client.py))
declares **every** parameter as `Optional[str]` — including `smart_format`, `interim_results`, `channels`, `sample_rate`, and `endpointing`:

```python
smart_format: typing.Optional[str] = None,
interim_results: typing.Optional[str] = None,
channels: typing.Optional[str] = None,
sample_rate: typing.Optional[str] = None,
endpointing: typing.Optional[str] = None,
```

The query-string pipeline is:

```
caller args → jsonable_encoder() → remove_none_from_dict() → encode_query() → urllib.parse.urlencode()
```

The `jsonable_encoder`
([source @ 74f48c51](https://raw.githubusercontent.com/deepgram/deepgram-python-sdk/74f48c51/src/deepgram/core/jsonable_encoder.py))
contains this branch:

```python
if isinstance(obj, (str, int, float, type(None))):
    return obj          # returned unchanged
```

Because `bool` is a subclass of `int` in Python, `isinstance(True, int)` is `True`.
So `jsonable_encoder(True)` returns the Python `bool` object `True` unmodified.

`urllib.parse.urlencode` then converts each value with `str()`:

```python
str(True)  → "True"   # ← what the SDK produces
str(False) → "False"
```

Deepgram's API only accepts lowercase JSON-style booleans (`"true"` / `"false"`).
The two offending parameters in the captured URL are:

```
smart_format=True      ← must be smart_format=true
interim_results=True   ← must be interim_results=true
```

These are **the only invalid parameters**. The rest of the query string is fine:

| Parameter | Captured value | Status |
|---|---|---|
| `model` | `nova-3` | ✅ valid model for `/v1/listen` |
| `encoding` | `linear16` | ✅ valid |
| `sample_rate` | `16000` | ✅ int serialises correctly |
| `channels` | `1` | ✅ int serialises correctly |
| `endpointing` | `300` | ✅ int serialises correctly |
| `language` | `en-US` | ✅ valid |
| `smart_format` | `True` | ❌ must be `"true"` |
| `interim_results` | `True` | ❌ must be `"true"` |

---

## 2. Correct Call Pattern for deepgram-sdk 6.1.1

All parameters are `Optional[str]`. Pass booleans as lowercase string literals;
numeric parameters can stay as Python `int` (they serialise correctly via `str()`):

```python
with dg_client.listen.v1.connect(
    model="nova-3",
    encoding="linear16",
    sample_rate=16000,        # int is fine → "16000"
    channels=1,               # int is fine → "1"
    language="en-US",
    smart_format="true",      # str, NOT bool True
    interim_results="true",   # str, NOT bool True
    endpointing=300,          # int is fine → "300"
) as conn:
    ...
```

This produces the correct query string:

```
/v1/listen?channels=1&encoding=linear16&endpointing=300&interim_results=true&language=en-US&model=nova-3&sample_rate=16000&smart_format=true
```

---

## 3. Investigation Methodology

### How to look up SDK source for a given version

1. **PyPI source link** → `https://pypi.org/project/deepgram-sdk/6.1.1/` → Repository link → GitHub.
2. **Find the exact commit for a PyPI release:** `CHANGELOG.md` or GitHub tags.  
   For 6.1.1, commit `74f48c51` was identified from the repository.
3. **Raw file access** (GitHub web viewer often returns empty content):
   ```
   https://raw.githubusercontent.com/deepgram/deepgram-python-sdk/<commit>/src/deepgram/listen/v1/client.py
   ```
4. **SDK is Fern-generated** (stated in README since v6). Check `src/deepgram/core/` for
   `jsonable_encoder.py`, `query_encoder.py`, `remove_none_from_dict.py` — these are
   the serialisation primitives that govern every query string.

### Most useful URLs for SDK/API mismatch debugging

| Resource | URL |
|---|---|
| PyPI release page | `https://pypi.org/project/deepgram-sdk/<version>/` |
| SDK `listen/v1/client.py` | `raw.githubusercontent.com/…/src/deepgram/listen/v1/client.py` |
| SDK `core/jsonable_encoder.py` | `raw.githubusercontent.com/…/src/deepgram/core/jsonable_encoder.py` |
| Deepgram live API reference | `https://developers.deepgram.com/reference/speech-to-text-api/listen-streaming` |
| Deepgram live streaming guide | `https://developers.deepgram.com/docs/getting-started-with-live-streaming-audio` |
| SDK GitHub issues (search "400") | `https://github.com/deepgram/deepgram-python-sdk/issues` |

### General pattern: "which query param is wrong" when error is vague

1. **Capture the full URL** before it hits the server — enable `websockets` debug
   logging (`logging.basicConfig(level=logging.DEBUG)`) to see the `GET` line.
2. **Diff against the API reference** parameter-by-parameter. Check:
   - Boolean literals: `true`/`false` (API) vs `True`/`False` (Python `str()`).
   - Integer params typed as `str` in the SDK — pass as int or string, confirm
     the value survives `urlencode` correctly.
   - Enum/model names: exact case, hyphens vs underscores.
3. **Read the SDK type signatures** (`Optional[str]` vs `Optional[bool]`) — a
   type mismatch here is a strong signal that the SDK was Fern-generated from a
   string-based schema, and booleans must be passed as `"true"` / `"false"`.
4. **Bisect**: strip all optional params, confirm connection succeeds with just
   `model=`, then add back one param at a time to identify the offender.
