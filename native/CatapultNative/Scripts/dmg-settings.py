app = defines["app"]
background = defines["background"]
sync_setup = defines.get("sync_setup")
encrypted_sync = defines.get("encrypted_sync")

format = "UDZO"
filesystem = "HFS+"
size = "180M"

files = [app]
if sync_setup:
    files.append(sync_setup)
if encrypted_sync:
    files.append(encrypted_sync)
symlinks = {"Applications": "/Applications"}

window_rect = ((120, 120), (760, 500))
default_view = "icon-view"
show_toolbar = False
show_status_bar = False

icon_size = 96
text_size = 13
arrange_by = None

icon_locations = {
    "Catapult.app": (208, 230),
    "Applications": (552, 230),
}

if sync_setup:
    icon_locations["Configure Catapult Sync.command"] = (380, 380)
