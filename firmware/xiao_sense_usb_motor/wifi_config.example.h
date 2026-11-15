#pragma once

// Copy this file to wifi_config.h in the same folder and fill it in.
// wifi_config.h is gitignored. The hotspot name and password are compiled
// into the firmware, so change them here and flash once over USB.

#define RETRACE_WIFI_SSID "phone-hotspot-name"
#define RETRACE_WIFI_PASSWORD "phone-hotspot-password"
// Laptop address on that hotspot. Leave empty to use retrace.local only.
#define RETRACE_HOST "192.168.0.0"
// Must match ring_wifi_port on the laptop (default 8011).
#define RETRACE_PORT 8011
