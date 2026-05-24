# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for ModBot.exe
# Build: pyinstaller build.spec
#
from PyInstaller.utils.hooks import collect_all, collect_data_files

# Collect everything uvicorn and discord.py ship — they have many dynamic
# imports and data files that static analysis misses.
uvicorn_datas, uvicorn_binaries, uvicorn_hidden = collect_all('uvicorn')
discord_datas, discord_binaries, discord_hidden = collect_all('discord')
aiohttp_datas, aiohttp_binaries, aiohttp_hidden = collect_all('aiohttp')

a = Analysis(
    ['bot.py'],
    pathex=[],
    binaries=uvicorn_binaries + discord_binaries + aiohttp_binaries,
    datas=[
        # Dashboard UI files (templates + static assets)
        ('dashboard/templates', 'dashboard/templates'),
        ('dashboard/static',    'dashboard/static'),
    ] + uvicorn_datas + discord_datas + aiohttp_datas,
    hiddenimports=[
        # ── Cogs (loaded via load_extension string, not detected by PyInstaller) ──
        'cogs.spam',
        'cogs.slurs',
        'cogs.nuke_protection',
        'cogs.join_protection',
        'cogs.link_filter',
        'cogs.admin',
        'cogs.settings',
        # ── Utils ──
        'utils.actions',
        'utils.embeds',
        'utils.fuzzy_match',
        'utils.views',
        # ── Uvicorn internals ──
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        # ── FastAPI / Starlette ──
        'starlette.middleware',
        'starlette.middleware.base',
        'starlette.routing',
        'starlette.staticfiles',
        'starlette.templating',
        'starlette.responses',
        # ── Form parsing ──
        'multipart',
        'multipart.multipart',
        # ── Crypto (token_store.py) ──
        'cryptography.hazmat.primitives.kdf.pbkdf2',
        'cryptography.hazmat.primitives.hashes',
        'cryptography.hazmat.backends',
        'cryptography.fernet',
        # ── Keyring ──
        'keyring',
        'keyring.backends',
        'keyring.backends.fail',
        # ── Async runtime ──
        'anyio',
        'anyio._backends._asyncio',
        'sniffio',
        # ── HTTP ──
        'h11',
        'httptools',
        'websockets',
        'websockets.legacy',
        # ── Misc ──
        'rapidfuzz',
        'rapidfuzz.fuzz',
        'itsdangerous',
        'jinja2.ext',
        'aiosqlite',
        'dotenv',
        'python_dotenv',
    ] + uvicorn_hidden + discord_hidden + aiohttp_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavy packages not needed at runtime
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
        'PIL',
        'PyQt5',
        'PyQt6',
        'wx',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ModBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # No terminal window — dashboard opens in browser
    windowed=True,      # macOS: proper .app bundle
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
