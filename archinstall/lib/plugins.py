import hashlib
import importlib
import logging
import os
import sys
import urllib.parse
import urllib.request
from importlib import metadata
from pathlib import Path
from typing import Optional, List

from .output import log
from .storage import storage

plugins = {}


# 1: List archinstall.plugin definitions
# 2: Load the plugin entrypoint
# 3: Initiate the plugin and store it as .name in plugins
for plugin_definition in metadata.entry_points().select(group='archinstall.plugin'):
	plugin_entrypoint = plugin_definition.load()

	try:
		plugins[plugin_definition.name] = plugin_entrypoint()
	except Exception as err:
		log(f'Error: {err}', level=logging.ERROR)
		log(f"The above error was detected when loading the plugin: {plugin_definition}", fg="red", level=logging.ERROR)


def localize_path(path: Path) -> Path:
	"""
	Support structures for load_plugin()
	"""
	url = urllib.parse.urlparse(str(path))

	if url.scheme and url.scheme in ('https', 'http'):
		converted_path = Path(f'/tmp/{path.stem}_{hashlib.md5(os.urandom(12)).hexdigest()}.py')

		with open(converted_path, "w") as temp_file:
			temp_file.write(urllib.request.urlopen(url.geturl()).read().decode('utf-8'))

		return converted_path
	else:
		return path


def import_via_path(path: Path, namespace: Optional[str] = None) -> Optional[str]:
	if not namespace:
		namespace = os.path.basename(path)

		if namespace == '__init__.py':
			namespace = path.parent.name

	try:
		spec = importlib.util.spec_from_file_location(namespace, path)
		if spec and spec.loader:
			imported = importlib.util.module_from_spec(spec)
			sys.modules[namespace] = imported
			spec.loader.exec_module(sys.modules[namespace])

		return namespace
	except Exception as err:
		log(f'Error: {err}', level=logging.ERROR)
		log(f"The above error was detected when loading the plugin: {path}", fg="red", level=logging.ERROR)

		try:
			del sys.modules[namespace]
		except Exception:
			pass

	return namespace


def find_nth(haystack: List[str], needle: str, n: int) -> Optional[int]:
	indices = [idx for idx, elem in enumerate(haystack) if elem == needle]
	if n <= len(indices):
		return indices[n - 1]
	return None


def load_plugin(path: Path):
	namespace: Optional[str] = None
	parsed_url = urllib.parse.urlparse(str(path))
	log(f"Loading plugin from url {parsed_url}.", level=logging.INFO)

	# The Profile was not a direct match on a remote URL
	if not parsed_url.scheme:
		# Path was not found in any known examples, check if it's an absolute path
		if os.path.isfile(path):
			namespace = import_via_path(path)
	elif parsed_url.scheme in ('https', 'http'):
		localized = localize_path(path)
		namespace = import_via_path(localized)

	if namespace and namespace in sys.modules:
		# Version dependency via __archinstall__version__ variable (if present) in the plugin
		# Any errors in version inconsistency will be handled through normal error handling if not defined.
		if hasattr(sys.modules[namespace], '__archinstall__version__'):
			archinstall_major_and_minor_version = float(storage['__version__'][:find_nth(storage['__version__'], '.', 2)])

			if sys.modules[namespace].__archinstall__version__ < archinstall_major_and_minor_version:
				log(f"Plugin {sys.modules[namespace]} does not support the current Archinstall version.", fg="red", level=logging.ERROR)

		# Locate the plugin entry-point called Plugin()
		# This in accordance with the entry_points() from setup.cfg above
		if hasattr(sys.modules[namespace], 'Plugin'):
			try:
				plugins[namespace] = sys.modules[namespace].Plugin()
				log(f"Plugin {plugins[namespace]} has been loaded.", fg="gray", level=logging.INFO)
			except Exception as err:
				log(f'Error: {err}', level=logging.ERROR)
				log(f"The above error was detected when initiating the plugin: {path}", fg="red", level=logging.ERROR)
		else:
			log(f"Plugin '{path}' is missing a valid entry-point or is corrupt.", fg="yellow", level=logging.WARNING)
