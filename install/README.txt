DESKTOP PET - ONE-CLICK INSTALL
===============================

Keep this "install" folder inside the Desktop Pet folder. If you downloaded a ZIP,
unzip it first. Don't move or delete the Desktop Pet folder afterwards, because the
shortcut runs the app from there. If you do move it, run the installer again.


WINDOWS
-------
Double-click:  Install Desktop Pet (Windows).bat

If Windows shows "Windows protected your PC", click More info > Run anyway.


MAC
---
Double-click:  Install Desktop Pet (Mac).command

The first time, macOS may refuse to open a downloaded file. If it does:
  Right-click (or Control-click) the file > Open > Open.
On recent macOS versions, use System Settings > Privacy & Security > Open Anyway.

If it says you don't have permission, open Terminal, type   chmod +x   followed by a
space, drag the file into the window, and press Return. Then double-click it again.


WHAT IT DOES
------------
1. Finds Python 3.10 or newer, and installs it if it's missing. If it can't, it
   tells you where to download it.
2. Creates a private Python environment, so nothing else on your computer changes:
     Windows:  %LOCALAPPDATA%\DesktopCat\venv
     Mac:      ~/Library/Application Support/DesktopCat/venv
3. Installs the packages Desktop Pet needs (needs internet, about 100 MB).
4. Creates a "Desktop Pet" icon on your Desktop and starts the pet.

Running it again is safe. It updates the packages and recreates the shortcut.


UNINSTALL
---------
Quit the pet (right-click it > Quit), delete the Desktop icon, delete the DesktopCat
folder listed above, and delete the Desktop Pet folder.
On Windows, also remove "Desktop Pet" from the Start menu.
