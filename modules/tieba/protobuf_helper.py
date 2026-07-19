import importlib.machinery
import importlib.util
import os
import sys
import types

_PROTO_DIR = os.path.join(os.path.dirname(__file__), "proto")

# Public flag: check this in parser.py
PROTO_AVAILABLE = False


def _clean_sys_modules():
    keys = [k for k in sys.modules if k.startswith("aiotieba")]
    for k in keys:
        del sys.modules[k]


def _bootstrap():
    global PROTO_AVAILABLE

    try:
        import google.protobuf  # noqa: F401
    except ImportError:
        return

    src_dir = os.path.abspath(_PROTO_DIR)
    if not os.path.isdir(os.path.join(src_dir, "aiotieba", "api", "_protobuf")):
        return

    aiotieba_dir = os.path.join(src_dir, "aiotieba")
    api_dir = os.path.join(aiotieba_dir, "api")
    proto_dir = os.path.join(api_dir, "_protobuf")
    posts_proto_dir = os.path.join(api_dir, "get_posts", "protobuf")

    for pkg_name, pkg_path in (
        ("aiotieba", [aiotieba_dir]),
        ("aiotieba.api", [api_dir]),
        ("aiotieba.api._protobuf", [proto_dir]),
        ("aiotieba.api.get_posts", [os.path.join(api_dir, "get_posts")]),
        ("aiotieba.api.get_posts.protobuf", [posts_proto_dir]),
    ):
        if pkg_name not in sys.modules:
            mod = types.ModuleType(pkg_name)
            mod.__package__ = pkg_name
            mod.__path__ = pkg_path
            sys.modules[pkg_name] = mod

    try:
        for src_root, mod_prefix in (
            (proto_dir, "aiotieba.api._protobuf"),
            (posts_proto_dir, "aiotieba.api.get_posts.protobuf"),
        ):
            for f_name in sorted(os.listdir(src_root)):
                if not f_name.endswith("_pb2.py") or f_name.startswith("_"):
                    continue
                mod_name = f"{mod_prefix}.{f_name[:-3]}"
                if mod_name not in sys.modules:
                    loader = importlib.machinery.SourceFileLoader(
                        mod_name,
                        os.path.join(src_root, f_name),
                    )
                    spec = importlib.util.spec_from_loader(mod_name, loader)
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[mod_name] = mod
                    loader.exec_module(mod)

        PROTO_AVAILABLE = True
    except Exception:
        _clean_sys_modules()


_bootstrap()
