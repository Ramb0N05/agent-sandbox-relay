#!/bin/sh
# VM anlegen auf dem CachyOS-Host (Abschnitt 6 des Canvas)
# Backing image bleibt unangetastet; Overlay liegt auf der SSD.
virt-install \
  --connect qemu:///system \
  --name vibe-sandbox \
  --memory 8192 --vcpus 4 \
  --cpu host-model \
  --machine q35 \
  --osinfo archlinux \
  --import \
  --disk path=/mnt/GamesSSD0/libvirt/images/vibe-sandbox.qcow2,size=64,format=qcow2,bus=virtio,backing_store=/mnt/GamesHDD/libvirt/images/Arch-Linux-x86_64-cloudimg.qcow2 \
  --network network=default,model=virtio \
  --graphics none \
  --memballoon none \
  --controller usb,model=none \
  --cloud-init user-data=/root/vibe-sandbox-user-data.yaml,disable=on
