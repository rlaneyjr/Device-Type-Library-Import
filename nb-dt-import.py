#!/usr/bin/env python3
from datetime import datetime

import settings
from netbox_api import NetBox


def _extract_manufacturers(entries):
    """Return a de-duplicated list of {'name': str, 'slug': str} dicts
    extracted from the manufacturer field of parsed device/module/rack types."""
    seen = set()
    manufacturers = []
    for entry in entries:
        mfr = entry.get('manufacturer')
        if not mfr or not isinstance(mfr, dict):
            continue
        slug = mfr.get('slug')
        if slug not in seen:
            seen.add(slug)
            manufacturers.append(mfr)
    return manufacturers


def main():
    startTime = datetime.now()
    args = settings.args

    netbox = NetBox(settings)

    # --- Device Types ---------------------------------------------------
    files, vendors = settings.dtl_repo.get_devices(
        f'{settings.dtl_repo.repo_path}/device-types/', args.vendors)
    settings.handle.log(f'{len(vendors)} Vendors Found')
    device_types = settings.dtl_repo.parse_files(files, slugs=args.slugs)
    settings.handle.log(f'{len(device_types)} Device-Types Found')

    # Ensure all manufacturers referenced by the parsed device types exist
    # BEFORE creating any device types that depend on them.
    netbox.create_manufacturers(_extract_manufacturers(device_types))
    netbox.create_device_types(device_types)

    # --- Module Types ----------------------------------------------------
    if netbox.modules:
        settings.handle.log("Modules Enabled. Creating Modules...")
        files, vendors = settings.dtl_repo.get_devices(
            f'{settings.dtl_repo.repo_path}/module-types/', args.vendors)
        settings.handle.log(f'{len(vendors)} Module Vendors Found')
        module_types = settings.dtl_repo.parse_files(files, slugs=args.slugs)
        settings.handle.log(f'{len(module_types)} Module-Types Found')
        netbox.create_manufacturers(_extract_manufacturers(module_types))
        netbox.create_module_types(module_types)

    # --- Rack Types ------------------------------------------------------
    if settings.NETBOX_FEATURES['rack_types'] and netbox.rack_types_enabled:
        settings.handle.log("Rack Types Enabled. Creating Rack Types...")
        files, vendors = settings.dtl_repo.get_devices(
            f'{settings.dtl_repo.repo_path}/rack-types/', args.vendors)
        settings.handle.log(f'{len(vendors)} Rack Vendors Found')
        rack_types = settings.dtl_repo.parse_files(files, slugs=args.slugs)
        settings.handle.log(f'{len(rack_types)} Rack-Types Found')
        netbox.create_manufacturers(_extract_manufacturers(rack_types))
        netbox.create_rack_types(rack_types)

    settings.handle.log('---')
    settings.handle.verbose_log(
        f'Script took {(datetime.now() - startTime)} to run')
    settings.handle.log(f'{netbox.counter["added"]} devices created')
    settings.handle.log(f'{netbox.counter["images"]} images uploaded')
    settings.handle.log(
        f'{netbox.counter["updated"]} interfaces/ports updated')
    settings.handle.log(
        f'{netbox.counter["manufacturer"]} manufacturers created')
    if settings.NETBOX_FEATURES['modules']:
        settings.handle.log(
            f'{netbox.counter["module_added"]} modules created')
        settings.handle.log(
            f'{netbox.counter["module_port_added"]} module interface / ports created')
    if settings.NETBOX_FEATURES['rack_types']:
        settings.handle.log(
            f'{netbox.counter["rack_added"]} rack types created')


if __name__ == "__main__":
    main()
