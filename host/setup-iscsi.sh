#!/usr/bin/env bash
# setup-iscsi.sh — attach the Synology iSCSI LUN that backs the CI engine stores.
#
# WHY THIS EXISTS
#   Docker and BuildKit both use overlayfs for their layer stores, and the kernel
#   refuses to mount overlayfs with an NFS upperdir:
#       mount ... upperdir=/var/lib/docker/.../snapshots/4/fs ... err: invalid argument
#   (verified on this box, 5.15.0-186-generic). So engine state needs a real
#   block device. This attaches one over the same fiber link as the NFS mount.
#
# RUN AS:  sudo ~/k8s/arc/setup-iscsi.sh
#   sudo is NOT passwordless for these commands, so the agent cannot run this
#   unattended — the owner runs it.
#
# ============================ READ THIS FIRST ============================
# Start with an inspection pass. It logs in, shows every LUN on the target,
# and logs back out WITHOUT writing anything:
#
#     sudo INSPECT=1 ~/k8s/arc/setup-iscsi.sh
#
# Then run for real once you know which LUN is the right one.
# ========================================================================
#
# ENV OVERRIDES
#   TARGET=<iqn>     use this target instead of auto-detecting
#   DEVICE=<path>    use this exact block device (required if the target has
#                    more than one LUN)
#   PORTAL=<ip>      portal to log in through   (default 10.100.0.5, the fiber
#                    link that the NAS source-IP whitelist expects)
#   FORMAT_OK=yes    skip the interactive format confirmation
#   INSPECT=1        look, report, log out, change nothing
#
# SAFETY RULES THIS SCRIPT FOLLOWS
#   * Never formats a device that carries any filesystem or blkid signature.
#   * Never mounts a pre-existing filesystem unless its label is exactly
#     "arc-block" — so it cannot wander into unrelated data on a shared target.
#   * Refuses to guess when a target exposes multiple LUNs.

set -euo pipefail

PORTAL="${PORTAL:-10.100.0.5}"
MOUNT="${MOUNT:-/mnt/arc-block}"
LABEL="arc-block"
SUBDIRS=(docker buildkit externals work)

die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '\n=== %s ===\n' "$*"; }

[ "$(id -u)" -eq 0 ] || die "must run as root:  sudo $0"

# ---------------------------------------------------------------- initiator
info "iSCSI initiator"
if [ ! -s /etc/iscsi/initiatorname.iscsi ]; then
    install -d -m 755 /etc/iscsi
    printf 'InitiatorName=%s\n' "$(/usr/sbin/iscsi-iname)" > /etc/iscsi/initiatorname.iscsi
    echo "generated a new initiator name"
fi
IQN=$(sed -n 's/^InitiatorName=//p' /etc/iscsi/initiatorname.iscsi)
echo "InitiatorName: $IQN"

systemctl enable --now iscsid
systemctl enable --now open-iscsi 2>/dev/null || true

# ---------------------------------------------------------------- discovery
info "Discovering targets on $PORTAL"
if ! timeout 10 bash -c "cat < /dev/null > /dev/tcp/$PORTAL/3260" 2>/dev/null; then
    die "$PORTAL:3260 is not reachable. Enable the iSCSI target in DSM and make
       sure this initiator is permitted:
           $IQN"
fi

# The NAS answers on several interfaces with the same IQN; we only ever log in
# through $PORTAL so we don't end up with duplicate devices for one LUN.
DISCOVERED=$(iscsiadm -m discovery -t sendtargets -p "$PORTAL" | awk '{print $2}' | sort -u)
echo "$DISCOVERED" | sed 's/^/  /'

if [ -n "${TARGET:-}" ]; then
    echo "using TARGET override: $TARGET"
else
    COUNT=$(echo "$DISCOVERED" | grep -c . || true)
    if [ "$COUNT" -eq 1 ]; then
        TARGET=$(echo "$DISCOVERED" | head -1)
        echo "one target found: $TARGET"
    else
        die "several targets found. Pick one and re-run with:
           sudo TARGET=<iqn> $0"
    fi
fi

# ---------------------------------------------------------------- login
info "Logging in via $PORTAL"
iscsiadm -m node -T "$TARGET" -p "$PORTAL:3260" --op update -n node.startup -v automatic
iscsiadm -m node -T "$TARGET" -p "$PORTAL:3260" --login 2>/dev/null || echo "(already logged in)"

echo "waiting for LUNs to appear..."
sleep 3
udevadm settle 2>/dev/null || true

mapfile -t LUNS < <(ls -1 /dev/disk/by-path/ 2>/dev/null \
                    | grep -F "ip-$PORTAL:3260-iscsi-$TARGET-lun-" | sort || true)
[ "${#LUNS[@]}" -gt 0 ] || die "no LUNs appeared for $TARGET.
       The target exists but nothing is mapped to this initiator. In DSM, map a
       LUN to this target and permit initiator:
           $IQN"

info "LUNs on this target"
for l in "${LUNS[@]}"; do
    R=$(readlink -f "/dev/disk/by-path/$l")
    SZ=$(lsblk -dno SIZE "$R" 2>/dev/null || echo "?")
    FS=$(blkid -o value -s TYPE "$R" 2>/dev/null || echo "(empty)")
    LB=$(blkid -o value -s LABEL "$R" 2>/dev/null || echo "-")
    MP=$(lsblk -dno MOUNTPOINT "$R" 2>/dev/null || echo "")
    printf '  %-70s -> %-9s size=%-8s fs=%-10s label=%-12s %s\n' \
           "$l" "$R" "$SZ" "$FS" "$LB" "${MP:+mounted at $MP}"
done

# ---------------------------------------------------------------- inspect mode
if [ "${INSPECT:-}" = "1" ]; then
    info "INSPECT mode — logging back out, nothing was changed"
    iscsiadm -m node -T "$TARGET" -p "$PORTAL:3260" --op update -n node.startup -v manual
    iscsiadm -m node -T "$TARGET" -p "$PORTAL:3260" --logout >/dev/null 2>&1 || true
    cat <<EOF

Nothing was written. To proceed for real, pick the LUN you want and run:

    sudo DEVICE=<device> FORMAT_OK=yes $0

A LUN is safe to use here if it shows fs=(empty), or fs=ext4 with label=arc-block.
This script will refuse anything else.
EOF
    exit 0
fi

# ---------------------------------------------------------------- pick device
info "Selecting device"
if [ -n "${DEVICE:-}" ]; then
    REAL=$(readlink -f "$DEVICE")
    [ -b "$REAL" ] || die "DEVICE=$DEVICE is not a block device"
    echo "using DEVICE override: $DEVICE -> $REAL"
elif [ "${#LUNS[@]}" -eq 1 ]; then
    REAL=$(readlink -f "/dev/disk/by-path/${LUNS[0]}")
    echo "single LUN: $REAL"
else
    die "this target exposes ${#LUNS[@]} LUNs — refusing to guess.
       Re-run naming the one you want:
           sudo DEVICE=/dev/disk/by-path/<one-of-the-above> $0"
fi

# ---------------------------------------------------------------- format / reuse
info "Filesystem"
FSTYPE=$(blkid -o value -s TYPE "$REAL" 2>/dev/null || true)
FSLABEL=$(blkid -o value -s LABEL "$REAL" 2>/dev/null || true)

if [ -z "$FSTYPE" ]; then
    if blkid -p "$REAL" 2>/dev/null | grep -q .; then
        die "$REAL reports no filesystem but blkid sees signatures on it.
       Refusing to format. Inspect by hand:  blkid -p $REAL"
    fi
    echo "$REAL is empty."
    if [ "${FORMAT_OK:-}" = "yes" ]; then
        echo "FORMAT_OK=yes — formatting without prompting."
    elif [ -t 0 ]; then
        read -r -p "Format $REAL as ext4? This ERASES it. Type 'yes': " ANS
        [ "$ANS" = "yes" ] || die "aborted; nothing was written"
    else
        die "$REAL is unformatted and there is no terminal to confirm on.
       Re-run with:  sudo FORMAT_OK=yes DEVICE=$REAL $0"
    fi
    mkfs.ext4 -L "$LABEL" "$REAL"
else
    # Reuse only something this script itself created. Anything else on a shared
    # target is somebody's data.
    echo "$REAL already holds $FSTYPE (label='${FSLABEL:-none}')"
    [ "$FSTYPE" = "ext4" ] || die "expected ext4 or an empty device, found $FSTYPE. Refusing."
    [ "$FSLABEL" = "$LABEL" ] || die "this ext4 filesystem is labelled '${FSLABEL:-none}', not '$LABEL'.
       That means it is NOT a filesystem this script created, so it may hold
       unrelated data. Refusing to mount or modify it.
       If you are certain it is the right one:  e2label $REAL $LABEL   then re-run."
    echo "label matches — reusing it."
fi

UUID=$(blkid -o value -s UUID "$REAL")
echo "UUID: $UUID"

# ---------------------------------------------------------------- mount
info "Mounting at $MOUNT"
mkdir -p "$MOUNT"
if ! grep -q "$UUID" /etc/fstab; then
    # _netdev so systemd waits for the network; nofail so a NAS outage cannot
    # wedge the boot of a box that hosts its own remote agent.
    printf 'UUID=%s  %s  ext4  _netdev,nofail,noatime  0  2\n' "$UUID" "$MOUNT" >> /etc/fstab
    echo "added to /etc/fstab"
    systemctl daemon-reload
fi
mountpoint -q "$MOUNT" || mount "$MOUNT"
mountpoint -q "$MOUNT" || die "mount failed"
df -h "$MOUNT" | tail -1

# ---------------------------------------------------------------- subdirs
# These live INSIDE the mounted filesystem on purpose. The pod spec uses
# hostPath type: Directory, so if the LUN is ever not mounted these paths won't
# exist and runner pods fail loudly rather than silently writing to the local
# 931 GB spinning disk.
info "Creating engine-store subdirectories"
for d in "${SUBDIRS[@]}"; do
    mkdir -p "$MOUNT/$d"
    chmod 777 "$MOUNT/$d"   # runner pods run as uid 1001, dind as root
    echo "  $MOUNT/$d"
done

info "Done"
cat <<EOF
Mount:      $MOUNT  ($(df -h "$MOUNT" | awk 'NR==2{print $2" total, "$4" free"}'))
Device:     $REAL
Initiator:  $IQN
Target:     $TARGET  via $PORTAL

Next:  kubectl apply -f ~/k8s/arc/20-scale-set.yaml
EOF
