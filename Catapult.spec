# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Catapult.app — macOS native sideloading tool."""

import os
import sys
from pathlib import Path

block_cipher = None
project_root = os.path.dirname(os.path.abspath(SPEC))

a = Analysis(
    [os.path.join(project_root, 'run.py')],
    pathex=[project_root],
    binaries=[],
    datas=[
        (os.path.join(project_root, 'static'), 'static'),
    ],
    hiddenimports=[
        # FastAPI / Starlette / Uvicorn internals that PyInstaller misses
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        'uvloop',
        'httptools',
        'websockets',
        'starlette.responses',
        'starlette.routing',
        'starlette.middleware',
        'multipart',
        'multipart.multipart',
        # Catapult modules
        'catapult.server',
        'catapult.main',
        'catapult.apple_auth',
        'catapult.anisette',
        'catapult.developer',
        'catapult.device',
        'catapult.ipa',
        'catapult.signer',
        'catapult.refresh',
        # pywebview macOS backend
        'webview',
        'webview.platforms',
        'webview.platforms.cocoa',
        # Crypto / network
        'cryptography',
        'srp',
        'zeroconf',
        'truststore',
        # pyobjc
        'objc',
        'Foundation',
        'AppKit',
        'WebKit',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Catapult',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='Catapult',
)

app = BUNDLE(
    coll,
    name='Catapult.app',
    icon=os.path.join(project_root, 'Catapult.icns'),
    bundle_identifier='com.catapult.app',
    info_plist={
        'CFBundleName': 'Catapult',
        'CFBundleDisplayName': 'Catapult',
        'CFBundleVersion': '0.3.8',
        'CFBundleShortVersionString': '0.3.8',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '13.0',
        'NSLocalNetworkUsageDescription': 'Catapult needs local network access to discover and communicate with iOS/tvOS devices.',
        'NSBonjourServices': ['_apple-mobdev2._tcp'],
    },
)
