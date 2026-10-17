# PyInstaller recipe for PulseDeck.exe — a folder with PulseDeck.exe in it,
# zipped by .github/workflows/release.yml.
#
#   pyinstaller --noconfirm PulseDeck.spec
#
# One folder, not --onefile: a one-file exe unpacks itself to a temp folder on
# every start (slower, and antivirus software flags that pattern more often).
# Settings, history and .env never live in this folder — see app_paths.py.

a = Analysis(
    ["monitor.py"],
    pathex=[SPECPATH],
    datas=[("assets", "assets")],
    # pystray picks its backend by name at run time; PyInstaller cannot see it.
    hiddenimports=["pystray._win32"],
    # PNG charts are not drawn any more (plot_history.py is a separate tool),
    # so matplotlib and numpy would only add ~60 MB.
    excludes=["matplotlib", "numpy", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PulseDeck",
    icon="assets/icon.ico",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PulseDeck", upx=False)
