from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING, Optional, List, Tuple

from .. import disk
from ..hardware import has_uefi
from ..menu import Menu, MenuSelectionType, TableMenu
from ..output import FormattedOutput
from ..output import log
from ..utils.util import prompt_dir

if TYPE_CHECKING:
	_: Any


def select_devices(preset: List[disk.BDevice] = []) -> List[disk.BDevice]:
	"""
	Asks the user to select one or multiple devices

	:return: List of selected devices
	:rtype: list
	"""

	def _preview_device_selection(selection: disk._DeviceInfo) -> Optional[str]:
		dev = disk.device_handler.get_device(selection.path)
		if dev and dev.partition_infos:
			return FormattedOutput.as_table(dev.partition_infos)
		return None

	if preset is None:
		preset = []

	title = str(_('Select one or more devices to use and configure'))
	warning = str(_('If you reset the device selection this will also reset the current disk layout. Are you sure?'))

	devices = disk.device_handler.devices
	options = [d.device_info for d in devices]
	preset_value = [p.device_info for p in preset]

	choice = TableMenu(
		title,
		data=options,
		multi=True,
		preset=preset_value,
		preview_command=_preview_device_selection,
		preview_title=str(_('Existing Partitions')),
		preview_size=0.2,
		allow_reset=True,
		allow_reset_warning_msg=warning
	).run()

	match choice.type_:
		case MenuSelectionType.Reset: return []
		case MenuSelectionType.Skip: return preset
		case MenuSelectionType.Selection:
			selected_device_info: List[disk._DeviceInfo] = choice.value  # type: ignore
			selected_devices = []

			for device in devices:
				if device.device_info in selected_device_info:
					selected_devices.append(device)

			return selected_devices


def get_default_partition_layout(
	devices: List[disk.BDevice],
	filesystem_type: Optional[disk.FilesystemType] = None,
	advanced_option: bool = False
) -> List[disk.DeviceModification]:

	if len(devices) == 1:
		device_modification = suggest_single_disk_layout(
			devices[0],
			filesystem_type=filesystem_type,
			advanced_options=advanced_option
		)
		return [device_modification]
	else:
		return suggest_multi_disk_layout(
			devices,
			filesystem_type=filesystem_type,
			advanced_options=advanced_option
		)


def _manual_partitioning(
	preset: List[disk.DeviceModification],
	devices: List[disk.BDevice]
) -> List[disk.DeviceModification]:
	modifications = []
	for device in devices:
		mod = next(filter(lambda x: x.device == device, preset), None)
		if not mod:
			mod = disk.DeviceModification(device, wipe=False)

		if partitions := disk.manual_partitioning(device, preset=mod.partitions):
			mod.partitions = partitions
			modifications.append(mod)

	return modifications


def select_disk_config(
	preset: Optional[disk.DiskLayoutConfiguration] = None,
	advanced_option: bool = False
) -> Optional[disk.DiskLayoutConfiguration]:
	default_layout = disk.DiskLayoutType.Default.display_msg()
	manual_mode = disk.DiskLayoutType.Manual.display_msg()
	pre_mount_mode = disk.DiskLayoutType.Pre_mount.display_msg()

	options = [default_layout, manual_mode, pre_mount_mode]
	preset_value = preset.config_type.display_msg() if preset else None
	warning = str(_('Are you sure you want to reset this setting?'))

	choice = Menu(
		_('Select a partitioning option'),
		options,
		allow_reset=True,
		allow_reset_warning_msg=warning,
		sort=False,
		preview_size=0.2,
		preset_values=preset_value
	).run()

	match choice.type_:
		case MenuSelectionType.Skip: return preset
		case MenuSelectionType.Reset: return None
		case MenuSelectionType.Selection:
			if choice.single_value == pre_mount_mode:
				output = "You will use whatever drive-setup is mounted at the specified directory\n"
				output += "WARNING: Archinstall won't check the suitability of this setup\n"

				path = prompt_dir(str(_('Enter the root directory of the mounted devices: ')), output)
				mods = disk.device_handler.detect_pre_mounted_mods(path)

				return disk.DiskLayoutConfiguration(
					config_type=disk.DiskLayoutType.Pre_mount,
					relative_mountpoint=path,
					device_modifications=mods
				)

			preset_devices = [mod.device for mod in preset.device_modifications] if preset else []

			devices = select_devices(preset_devices)

			if not devices:
				return None

			if choice.value == default_layout:
				modifications = get_default_partition_layout(devices, advanced_option=advanced_option)
				if modifications:
					return disk.DiskLayoutConfiguration(
						config_type=disk.DiskLayoutType.Default,
						device_modifications=modifications
					)
			elif choice.value == manual_mode:
				preset_mods = preset.device_modifications if preset else []
				modifications = _manual_partitioning(preset_mods, devices)

				if modifications:
					return disk.DiskLayoutConfiguration(
						config_type=disk.DiskLayoutType.Manual,
						device_modifications=modifications
					)

	return None


def _boot_partition() -> disk.PartitionModification:
	if has_uefi():
		start = disk.Size(1, disk.Unit.MiB)
		size = disk.Size(512, disk.Unit.MiB)
	else:
		start = disk.Size(3, disk.Unit.MiB)
		size = disk.Size(203, disk.Unit.MiB)

	# boot partition
	return disk.PartitionModification(
		status=disk.ModificationStatus.Create,
		type=disk.PartitionType.Primary,
		start=start,
		length=size,
		mountpoint=Path('/boot'),
		fs_type=disk.FilesystemType.Fat32,
		flags=[disk.PartitionFlag.Boot]
	)


def ask_for_main_filesystem_format(advanced_options=False) -> disk.FilesystemType:
	options = {
		'btrfs': disk.FilesystemType.Btrfs,
		'ext4': disk.FilesystemType.Ext4,
		'xfs': disk.FilesystemType.Xfs,
		'f2fs': disk.FilesystemType.F2fs
	}

	if advanced_options:
		options.update({'ntfs': disk.FilesystemType.Ntfs})

	prompt = _('Select which filesystem your main partition should use')
	choice = Menu(prompt, options, skip=False, sort=False).run()
	return options[choice.single_value]


def suggest_single_disk_layout(
	device: disk.BDevice,
	filesystem_type: Optional[disk.FilesystemType] = None,
	advanced_options: bool = False,
	separate_home: Optional[bool] = None
) -> disk.DeviceModification:
	if not filesystem_type:
		filesystem_type = ask_for_main_filesystem_format(advanced_options)

	min_size_to_allow_home_part = disk.Size(40, disk.Unit.GiB)
	root_partition_size = disk.Size(20, disk.Unit.GiB)
	using_subvolumes = False
	using_home_partition = False
	compression = False
	device_size_gib = device.device_info.total_size

	if filesystem_type == disk.FilesystemType.Btrfs:
		prompt = str(_('Would you like to use BTRFS subvolumes with a default structure?'))
		choice = Menu(prompt, Menu.yes_no(), skip=False, default_option=Menu.yes()).run()
		using_subvolumes = choice.value == Menu.yes()

		prompt = str(_('Would you like to use BTRFS compression?'))
		choice = Menu(prompt, Menu.yes_no(), skip=False, default_option=Menu.yes()).run()
		compression = choice.value == Menu.yes()

	device_modification = disk.DeviceModification(device, wipe=True)

	# Used for reference: https://wiki.archlinux.org/title/partitioning
	# 2 MiB is unallocated for GRUB on BIOS. Potentially unneeded for other bootloaders?

	# TODO: On BIOS, /boot partition is only needed if the drive will
	# be encrypted, otherwise it is not recommended. We should probably
	# add a check for whether the drive will be encrypted or not.

	# Increase the UEFI partition if UEFI is detected.
	# Also re-align the start to 1MiB since we don't need the first sectors
	# like we do in MBR layouts where the boot loader is installed traditionally.

	boot_partition = _boot_partition()
	device_modification.add_partition(boot_partition)

	if not using_subvolumes:
		if device_size_gib >= min_size_to_allow_home_part:
			if separate_home is None:
				prompt = str(_('Would you like to create a separate partition for /home?'))
				choice = Menu(prompt, Menu.yes_no(), skip=False, default_option=Menu.yes()).run()
				using_home_partition = choice.value == Menu.yes()
			elif separate_home is True:
				using_home_partition = True
			else:
				using_home_partition = False

	# root partition
	start = disk.Size(513, disk.Unit.MiB) if has_uefi() else disk.Size(206, disk.Unit.MiB)

	# Set a size for / (/root)
	if using_subvolumes or device_size_gib < min_size_to_allow_home_part or not using_home_partition:
		length = disk.Size(100, disk.Unit.Percent, total_size=device.device_info.total_size)
	else:
		length = min(device.device_info.total_size, root_partition_size)

	root_partition = disk.PartitionModification(
		status=disk.ModificationStatus.Create,
		type=disk.PartitionType.Primary,
		start=start,
		length=length,
		mountpoint=Path('/') if not using_subvolumes else None,
		fs_type=filesystem_type,
		mount_options=['compress=zstd'] if compression else [],
	)
	device_modification.add_partition(root_partition)

	if using_subvolumes:
		# https://btrfs.wiki.kernel.org/index.php/FAQ
		# https://unix.stackexchange.com/questions/246976/btrfs-subvolume-uuid-clash
		# https://github.com/classy-giraffe/easy-arch/blob/main/easy-arch.sh
		subvolumes = [
			disk.SubvolumeModification(Path('@'), Path('/')),
			disk.SubvolumeModification(Path('@home'), Path('/home')),
			disk.SubvolumeModification(Path('@log'), Path('/var/log')),
			disk.SubvolumeModification(Path('@pkg'), Path('/var/cache/pacman/pkg')),
			disk.SubvolumeModification(Path('@.snapshots'), Path('/.snapshots'))
		]
		root_partition.btrfs_subvols = subvolumes
	elif using_home_partition:
		# If we don't want to use subvolumes,
		# But we want to be able to re-use data between re-installs..
		# A second partition for /home would be nice if we have the space for it
		home_partition = disk.PartitionModification(
			status=disk.ModificationStatus.Create,
			type=disk.PartitionType.Primary,
			start=root_partition.length,
			length=disk.Size(100, disk.Unit.Percent, total_size=device.device_info.total_size),
			mountpoint=Path('/home'),
			fs_type=filesystem_type,
			mount_options=['compress=zstd'] if compression else []
		)
		device_modification.add_partition(home_partition)

	return device_modification


def suggest_multi_disk_layout(
	devices: List[disk.BDevice],
	filesystem_type: Optional[disk.FilesystemType] = None,
	advanced_options: bool = False
) -> List[disk.DeviceModification]:
	if not devices:
		return []

	# Not really a rock solid foundation of information to stand on, but it's a start:
	# https://www.reddit.com/r/btrfs/comments/m287gp/partition_strategy_for_two_physical_disks/
	# https://www.reddit.com/r/btrfs/comments/9us4hr/what_is_your_btrfs_partitionsubvolumes_scheme/
	min_home_partition_size = disk.Size(40, disk.Unit.GiB)
	# rough estimate taking in to account user desktops etc. TODO: Catch user packages to detect size?
	desired_root_partition_size = disk.Size(20, disk.Unit.GiB)
	compression = False

	if not filesystem_type:
		filesystem_type = ask_for_main_filesystem_format(advanced_options)

	# find proper disk for /home
	possible_devices = list(filter(lambda x: x.device_info.total_size >= min_home_partition_size, devices))
	home_device = max(possible_devices, key=lambda d: d.device_info.total_size) if possible_devices else None

	# find proper device for /root
	devices_delta = {}
	for device in devices:
		if device is not home_device:
			delta = device.device_info.total_size - desired_root_partition_size
			devices_delta[device] = delta

	sorted_delta: List[Tuple[disk.BDevice, Any]] = sorted(devices_delta.items(), key=lambda x: x[1])
	root_device: Optional[disk.BDevice] = sorted_delta[0][0]

	if home_device is None or root_device is None:
		text = _('The selected drives do not have the minimum capacity required for an automatic suggestion\n')
		text += _('Minimum capacity for /home partition: {}GiB\n').format(min_home_partition_size.format_size(disk.Unit.GiB))
		text += _('Minimum capacity for Arch Linux partition: {}GiB').format(desired_root_partition_size.format_size(disk.Unit.GiB))
		Menu(str(text), [str(_('Continue'))], skip=False).run()
		return []

	if filesystem_type == disk.FilesystemType.Btrfs:
		prompt = str(_('Would you like to use BTRFS compression?'))
		choice = Menu(prompt, Menu.yes_no(), skip=False, default_option=Menu.yes()).run()
		compression = choice.value == Menu.yes()

	device_paths = ', '.join([str(d.device_info.path) for d in devices])
	log(f"Suggesting multi-disk-layout for devices: {device_paths}", level=logging.DEBUG)
	log(f"/root: {root_device.device_info.path}", level=logging.DEBUG)
	log(f"/home: {home_device.device_info.path}", level=logging.DEBUG)

	root_device_modification = disk.DeviceModification(root_device, wipe=True)
	home_device_modification = disk.DeviceModification(home_device, wipe=True)

	# add boot partition to the root device
	boot_partition = _boot_partition()
	root_device_modification.add_partition(boot_partition)

	# add root partition to the root device
	root_partition = disk.PartitionModification(
		status=disk.ModificationStatus.Create,
		type=disk.PartitionType.Primary,
		start=disk.Size(513, disk.Unit.MiB) if has_uefi() else disk.Size(206, disk.Unit.MiB),
		length=disk.Size(100, disk.Unit.Percent, total_size=root_device.device_info.total_size),
		mountpoint=Path('/'),
		mount_options=['compress=zstd'] if compression else [],
		fs_type=filesystem_type
	)
	root_device_modification.add_partition(root_partition)

	# add home partition to home device
	home_partition = disk.PartitionModification(
		status=disk.ModificationStatus.Create,
		type=disk.PartitionType.Primary,
		start=disk.Size(1, disk.Unit.MiB),
		length=disk.Size(100, disk.Unit.Percent, total_size=home_device.device_info.total_size),
		mountpoint=Path('/home'),
		mount_options=['compress=zstd'] if compression else [],
		fs_type=filesystem_type,
	)
	home_device_modification.add_partition(home_partition)

	return [root_device_modification, home_device_modification]
