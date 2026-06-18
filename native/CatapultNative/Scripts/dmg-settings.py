app = defines["app"]
background = defines["background"]

format = "UDZO"
filesystem = "HFS+"
size = "140M"

files = [app]
symlinks = {"Applications": "/Applications"}

window_rect = ((120, 120), (760, 430))
default_view = "icon-view"
show_toolbar = False
show_status_bar = False

icon_size = 96
text_size = 13
arrange_by = None

icon_locations = {
    "Catapult.app": (208, 202),
    "Applications": (552, 202),
}
