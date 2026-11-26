/* afnative — CPython hot path for AutoFind's plain-text extraction.
 *
 * One call reads a text file, computes its SHA-256 and decodes it with the
 * exact encoding ladder of core/extract/plain.py. Python's buffered reader,
 * hashlib object and per-encoding trial/decoder objects cost ~0.4 ms per file
 * on a corpus of small texts; this module does the same work with one CRT
 * read and a static SHA-256. The GIL stays held because the decode helpers are
 * Python C API calls, but a process-pool worker has no contenders anyway.
 *
 * Pure-Python fallback lives in core/extract/plain.py; if the .pyd is absent
 * or fails to import, behaviour is unchanged.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#ifdef _WIN32
#include <windows.h>
#endif
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <wchar.h>

/* ------------------------------------------------------------------ SHA-256 */
typedef struct { uint32_t h[8]; uint64_t bits; unsigned char block[64]; size_t used; } SHA;

static const uint32_t K[64] = {
0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};

#define ROR(x,n) (((x) >> (n)) | ((x) << (32-(n))))
static void sha_block(SHA *s, const unsigned char *p) {
    uint32_t w[64], a,b,c,d,e,f,g,h;
    for (int i=0;i<16;i++) w[i] = ((uint32_t)p[i*4]<<24)|((uint32_t)p[i*4+1]<<16)|((uint32_t)p[i*4+2]<<8)|p[i*4+3];
    for (int i=16;i<64;i++) {
        uint32_t s0=ROR(w[i-15],7)^ROR(w[i-15],18)^(w[i-15]>>3);
        uint32_t s1=ROR(w[i-2],17)^ROR(w[i-2],19)^(w[i-2]>>10);
        w[i]=w[i-16]+s0+w[i-7]+s1;
    }
    a=s->h[0]; b=s->h[1]; c=s->h[2]; d=s->h[3]; e=s->h[4]; f=s->h[5]; g=s->h[6]; h=s->h[7];
    for (int i=0;i<64;i++) {
        uint32_t S1=ROR(e,6)^ROR(e,11)^ROR(e,25);
        uint32_t ch=(e&f)^((~e)&g);
        uint32_t t1=h+S1+ch+K[i]+w[i];
        uint32_t S0=ROR(a,2)^ROR(a,13)^ROR(a,22);
        uint32_t maj=(a&b)^(a&c)^(b&c);
        uint32_t t2=S0+maj;
        h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
    }
    s->h[0]+=a; s->h[1]+=b; s->h[2]+=c; s->h[3]+=d;
    s->h[4]+=e; s->h[5]+=f; s->h[6]+=g; s->h[7]+=h;
}
static void sha_init(SHA *s) {
    s->h[0]=0x6a09e667; s->h[1]=0xbb67ae85; s->h[2]=0x3c6ef372; s->h[3]=0xa54ff53a;
    s->h[4]=0x510e527f; s->h[5]=0x9b05688c; s->h[6]=0x1f83d9ab; s->h[7]=0x5be0cd19;
    s->bits=0; s->used=0;
}
static void sha_update(SHA *s, const unsigned char *p, size_t n) {
    s->bits += n;
    if (s->used) {
        size_t take = 64 - s->used;
        if (take > n) take = n;
        memcpy(s->block + s->used, p, take);
        s->used += take; p += take; n -= take;
        if (s->used == 64) { sha_block(s, s->block); s->used = 0; }
    }
    while (n >= 64) { sha_block(s, p); p += 64; n -= 64; }
    if (n) { memcpy(s->block, p, n); s->used = n; }
}
static PyObject *sha_digest(SHA *s) {
    unsigned char tail[128]; size_t n = s->used;
    memcpy(tail, s->block, n);
    tail[n++] = 0x80;
    if (n > 56) { memset(tail+n, 0, 64-n); sha_block(s, tail); n = 0; memset(tail, 0, 56); }
    memset(tail+n, 0, 56-n);
    uint64_t bits = s->bits * 8;
    for (int i=0;i<8;i++) tail[56+i] = (unsigned char)(bits >> (56 - i*8));
    sha_block(s, tail);
    unsigned char out[32];
    for (int i=0;i<8;i++) for (int j=0;j<4;j++) out[i*4+j] = (unsigned char)(s->h[i] >> (24-j*8));
    return PyBytes_FromStringAndSize((char*)out, 32);
}

/* ------------------------------------------------- plain.py's _looks_like_text */
static int looks_like_text(PyObject *text) {
    Py_ssize_t n = PyUnicode_GET_LENGTH(text);
    if (n > 4096) n = 4096;
    if (n == 0) return 1;
    int kind = PyUnicode_KIND(text);
    const void *data = PyUnicode_DATA(text);
    Py_ssize_t control = 0;
    for (Py_ssize_t i=0;i<n;i++) {
        Py_UCS4 ch = PyUnicode_READ(kind, data, i);
        if (ch < 32 && ch != '\n' && ch != '\r' && ch != '\t') control++;
    }
    return control * 50 < n;   /* control / len < 0.02 */
}

static PyObject *decode_with(PyObject *bytes, const char *enc, int *ok) {
    PyObject *text = PyUnicode_Decode(PyBytes_AS_STRING(bytes), PyBytes_GET_SIZE(bytes), enc, "strict");
    if (text == NULL) { PyErr_Clear(); *ok = 0; return NULL; }
    *ok = looks_like_text(text);
    return text;
}

/* Takes ownership of buf; both readers below hand their buffer here. */
static PyObject *af_finish(unsigned char *buf, size_t n) {
    SHA sha; sha_init(&sha); sha_update(&sha, buf, n);
    PyObject *digest = sha_digest(&sha);
    PyObject *bytes = PyBytes_FromStringAndSize((char*)buf, (Py_ssize_t)n);
    free(buf);
    if (!digest || !bytes) { Py_XDECREF(digest); Py_XDECREF(bytes); return NULL; }

    /* Same binary gate as extract_plain_bytes(): NUL in the first 4096 bytes. */
    size_t probe = n < 4096 ? n : 4096;
    PyObject *text = Py_None;
    if (memchr(PyBytes_AS_STRING(bytes), 0, probe) == NULL) {
        Py_ssize_t len = PyBytes_GET_SIZE(bytes);
        const char *raw = PyBytes_AS_STRING(bytes);
        int skip_bom = (len >= 3 && (unsigned char)raw[0]==0xEF && (unsigned char)raw[1]==0xBB && (unsigned char)raw[2]==0xBF) ? 3 : 0;
        PyObject *candidate = NULL;
        int ok = 0;
        if (skip_bom) {
            PyObject *view = PyBytes_FromStringAndSize(raw + skip_bom, len - skip_bom);
            if (view) { candidate = PyUnicode_DecodeUTF8(PyBytes_AS_STRING(view), PyBytes_GET_SIZE(view), "strict"); Py_DECREF(view); if (candidate) ok = looks_like_text(candidate); else PyErr_Clear(); }
        } else {
            candidate = decode_with(bytes, "utf-8", &ok);
        }
        if (!ok) { Py_XDECREF(candidate); candidate = decode_with(bytes, "cp1251", &ok); }
        if (!ok) { Py_XDECREF(candidate); candidate = decode_with(bytes, "koi8-r", &ok); }
        if (!ok) {
            Py_XDECREF(candidate);
            candidate = PyUnicode_DecodeLatin1(raw, len, "replace");
            if (candidate && !looks_like_text(candidate)) {
                /* plain.py's last resort is utf-8 + replace, not latin-1. */
                Py_DECREF(candidate);
                candidate = PyUnicode_DecodeUTF8(raw, len, "replace");
            }
        }
        if (candidate) text = candidate;
    }
    PyObject *result = Py_BuildValue("(OO)", text, digest);
    Py_DECREF(digest); Py_DECREF(bytes);
    if (text != Py_None) Py_DECREF(text);
    return result;
}

static int af_bad_cap(Py_ssize_t cap) {
    if (cap < 0) { PyErr_SetString(PyExc_ValueError, "negative max_bytes"); return 1; }
    return 0;
}

/* CRT reader — the portable baseline. */
static PyObject *af_read_text_digest(PyObject *self, PyObject *args) {
    PyObject *path_obj = NULL;
    Py_ssize_t cap = 20 * 1024 * 1024;
    if (!PyArg_ParseTuple(args, "U|n", &path_obj, &cap)) return NULL;
    if (af_bad_cap(cap)) return NULL;
    Py_ssize_t path_len = 0;
    const wchar_t *path = PyUnicode_AsWideCharString(path_obj, &path_len);
    if (!path) return NULL;
    FILE *f = _wfopen(path, L"rb");
    PyMem_Free((void*)path);
    if (!f) { PyErr_SetFromErrnoWithFilenameObjects(PyExc_OSError, PyTuple_GET_ITEM(args,0), NULL); return NULL; }
    size_t first = (cap < (1 << 20)) ? (size_t)cap : (size_t)(1 << 20);
    unsigned char *buf = (unsigned char*)malloc(first ? first : 1);
    if (!buf) { fclose(f); return PyErr_NoMemory(); }
    int oom = 0;
    size_t n = 0;
    /* The read is the slow part. Let other threads run while the disk works;
       a ThreadPool can then overlap I/O instead of paying for process IPC. */
    Py_BEGIN_ALLOW_THREADS
    n = fread(buf, 1, first, f);
    if (n == first && cap > (Py_ssize_t)first) {
        unsigned char *grown = (unsigned char*)realloc(buf, (size_t)cap);
        if (!grown) { oom = 1; }
        else {
            buf = grown;
            n += fread(buf + n, 1, (size_t)cap - n, f);
        }
    }
    fclose(f);
    Py_END_ALLOW_THREADS
    if (oom) { free(buf); return PyErr_NoMemory(); }
    return af_finish(buf, n);
}

/* Win32 reader: CreateFileW + ReadFile with FILE_FLAG_SEQUENTIAL_SCAN. On a
   corpus of small files this avoids the CRT FILE object layer entirely. */
static PyObject *af_read_text_digest_win32(PyObject *self, PyObject *args) {
    PyObject *path_obj = NULL;
    Py_ssize_t cap = 20 * 1024 * 1024;
    if (!PyArg_ParseTuple(args, "U|n", &path_obj, &cap)) return NULL;
    if (af_bad_cap(cap)) return NULL;
    Py_ssize_t path_len = 0;
    const wchar_t *path = PyUnicode_AsWideCharString(path_obj, &path_len);
    if (!path) return NULL;
    HANDLE h = CreateFileW(path, GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, NULL);
    PyMem_Free((void*)path);
    if (h == INVALID_HANDLE_VALUE) { PyErr_SetFromWindowsErr(0); return NULL; }
    size_t first = (cap < (1 << 20)) ? (size_t)cap : (size_t)(1 << 20);
    unsigned char *buf = (unsigned char*)malloc(first ? first : 1);
    if (!buf) { CloseHandle(h); return PyErr_NoMemory(); }
    DWORD got = 0;
    size_t n = 0;
    int oom = 0, failed = 0;
    Py_BEGIN_ALLOW_THREADS
    if (!ReadFile(h, buf, (DWORD)first, &got, NULL)) { failed = 1; }
    else {
        n = got;
        if (n == first && cap > (Py_ssize_t)first) {
            unsigned char *grown = (unsigned char*)realloc(buf, (size_t)cap);
            if (!grown) { oom = 1; }
            else {
                buf = grown;
                got = 0;
                if (!ReadFile(h, buf + n, (DWORD)((size_t)cap - n), &got, NULL)) failed = 1;
                else n += got;
            }
        }
    }
    CloseHandle(h);
    Py_END_ALLOW_THREADS
    if (oom) { free(buf); return PyErr_NoMemory(); }
    if (failed) { free(buf); return PyErr_SetFromWindowsErr(0); }
    return af_finish(buf, n);
}

static PyMethodDef methods[] = {
    {"read_text_digest", (PyCFunction)af_read_text_digest, METH_VARARGS,
     "read_text_digest(path: str, max_bytes: int) -> (str | None, bytes)\n"
     "Read a plain-text file once, return decoded text (or None for binary) and its SHA-256."},
#ifdef _WIN32
    {"read_text_digest_win32", (PyCFunction)af_read_text_digest_win32, METH_VARARGS,
     "Same result as read_text_digest, using CreateFileW + ReadFile instead of CRT stdio."},
#endif
    {NULL, NULL, 0, NULL}
};
static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "afnative", "AutoFind native hot path", -1, methods,
    NULL, NULL, NULL, NULL
};
PyMODINIT_FUNC PyInit_afnative(void) { return PyModule_Create(&module); }
