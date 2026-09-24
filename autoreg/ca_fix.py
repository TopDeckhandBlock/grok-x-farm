"""ca_fix — обход curl error 77 (CURLE_SSL_CACERT_BADFILE) на Windows.

curl_cffi на Windows ищет CA-бандл (обычно certifi). Если проект лежит по
не-ASCII пути (кириллица: «ИИ/грок тест»), libcurl не может открыть файл
и все HTTPS-запросы падают с curl(77). Фикс из v7: копия cacert.pem в
ASCII-путь (%TEMP%) + переменная CURL_CA_BUNDLE (проверено 21.09.26).

Импортировать ДО первого curl_cffi-запроса: `import ca_fix`.
"""

import os
import shutil
import sys
import tempfile


def ensure_ascii_ca_bundle() -> str | None:
    """Гарантирует ASCII-путь CA-бандла и выставляет CURL_CA_BUNDLE.

    Возвращает путь к бандлу или None, если certifi недоступен.
    Идемпотентно: уже выставленный CURL_CA_BUNDLE не трогает.
    """
    if os.environ.get("CURL_CA_BUNDLE"):
        return os.environ["CURL_CA_BUNDLE"]
    try:
        import certifi
        src = certifi.where()
    except Exception:
        return None
    try:
        if not os.path.exists(src):
            return None
        # путь уже ASCII — используем как есть
        if all(ord(c) < 128 for c in src):
            os.environ["CURL_CA_BUNDLE"] = src
            return src
        dst = os.path.join(tempfile.gettempdir(), "grok_cacert.pem")
        if not (os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src)):
            shutil.copyfile(src, dst)
        os.environ["CURL_CA_BUNDLE"] = dst
        return dst
    except Exception:
        return None


if sys.platform == "win32":
    ensure_ascii_ca_bundle()
