app = defines["app"]
background = defines["background"]

format = "UDZO"
filesystem = "HFS+"
size = "180M"

files = [app]
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
