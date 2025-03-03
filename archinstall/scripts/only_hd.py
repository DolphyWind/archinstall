import logging
import os
from pathlib import Path

import archinstall
from archinstall import Installer
from archinstall.lib.configuration import ConfigurationOutput
from archinstall import disk

if archinstall.arguments.get('help'):
	print("See `man archinstall` for help.")
	exit(0)


if os.getuid() != 0:
	print("Archinstall requires root privileges to run. See --help for more.")
	exit(1)


def ask_user_questions():
	global_menu = archinstall.GlobalMenu(data_store=archinstall.arguments)

	global_menu.enable('archinstall-language')

	global_menu.enable('disk_config', mandatory=True)
	global_menu.enable('disk_encryption')
	global_menu.enable('swap')

	global_menu.enable('save_config')
	global_menu.enable('install')
	global_menu.enable('abort')

	global_menu.run()


def perform_installation(mountpoint: Path):
	"""
	Performs the installation steps on a block device.
	Only requirement is that the block devices are
	formatted and setup prior to entering this function.
	"""
	disk_config: disk.DiskLayoutConfiguration = archinstall.arguments['disk_config']
	disk_encryption: disk.DiskEncryption = archinstall.arguments.get('disk_encryption', None)

	with Installer(
		mountpoint,
		disk_config,
		disk_encryption=disk_encryption,
		kernels=archinstall.arguments.get('kernels', ['linux'])
	) as installation:
		# Mount all the drives to the desired mountpoint
		# This *can* be done outside of the installation, but the installer can deal with it.
		if archinstall.arguments.get('disk_config'):
			installation.mount_ordered_layout()

		# to generate a fstab directory holder. Avoids an error on exit and at the same time checks the procedure
		target = Path(f"{mountpoint}/etc/fstab")
		if not target.parent.exists():
			target.parent.mkdir(parents=True)

	# For support reasons, we'll log the disk layout post installation (crash or no crash)
	archinstall.log(f"Disk states after installing: {disk.disk_layouts()}", level=logging.DEBUG)


# Log various information about hardware before starting the installation. This might assist in troubleshooting
archinstall.log(f"Hardware model detected: {archinstall.sys_vendor()} {archinstall.product_name()}; UEFI mode: {archinstall.has_uefi()}", level=logging.DEBUG)
archinstall.log(f"Processor model detected: {archinstall.cpu_model()}", level=logging.DEBUG)
archinstall.log(f"Memory statistics: {archinstall.mem_available()} available out of {archinstall.mem_total()} total installed", level=logging.DEBUG)
archinstall.log(f"Virtualization detected: {archinstall.virtualization()}; is VM: {archinstall.is_vm()}", level=logging.DEBUG)
archinstall.log(f"Graphics devices detected: {archinstall.graphics_devices().keys()}", level=logging.DEBUG)

# For support reasons, we'll log the disk layout pre installation to match against post-installation layout
archinstall.log(f"Disk states before installing: {disk.disk_layouts()}", level=logging.DEBUG)


if archinstall.arguments.get('skip-mirror-check', False) is False and archinstall.check_mirror_reachable() is False:
	log_file = os.path.join(archinstall.storage.get('LOG_PATH', None), archinstall.storage.get('LOG_FILE', None))
	archinstall.log(f"Arch Linux mirrors are not reachable. Please check your internet connection and the log file '{log_file}'.", level=logging.INFO, fg="red")
	exit(1)

if not archinstall.arguments.get('silent'):
	ask_user_questions()

config_output = ConfigurationOutput(archinstall.arguments)
if not archinstall.arguments.get('silent'):
	config_output.show()

config_output.save()

if archinstall.arguments.get('dry_run'):
	exit(0)

if not archinstall.arguments.get('silent'):
	input('Press Enter to continue.')

fs_handler = disk.FilesystemHandler(
	archinstall.arguments['disk_config'],
	archinstall.arguments.get('disk_encryption', None)
)

fs_handler.perform_filesystem_operations()


perform_installation(archinstall.storage.get('MOUNT_POINT', Path('/mnt')))
