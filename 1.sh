printf '%s\n' '--- X11 outputs ---'
xrandr --query

printf '%s\n' '--- DRM cards and connected outputs ---'
for p in /sys/class/drm/card*; do
  test -e "$p" || continue
  printf '%s -> ' "${p##*/}"
  readlink -f "$p/device" 2>/dev/null || true
done

for p in /sys/class/drm/card*-*/status; do
  test -f "$p" || continue
  printf '%s: ' "${p%/status}"
  cat "$p"
done

printf '%s\n' '--- KVM access ---'
test -r /dev/kvm && test -w /dev/kvm \
  && echo 'This user can access /dev/kvm' \
  || echo 'This user cannot access /dev/kvm'
