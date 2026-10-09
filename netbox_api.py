from collections import Counter
import pynetbox
import requests
import os
import glob

# Keys in module-type YAML that are component lists (not module-type API fields).
# Stripped before sending the module-type payload to the NetBox REST API.
MODULE_COMPONENT_KEYS = frozenset({
    'src', 'interfaces', 'power-ports', 'power-port',
    'console-ports', 'power-outlets', 'console-server-ports',
    'rear-ports', 'front-ports', 'module-bays', 'port-mappings',
})


class NetBox:
    def __new__(cls, *args, **kwargs):
        return super().__new__(cls)

    def __init__(self, settings):
        self.counter = Counter(
            added=0,
            updated=0,
            manufacturer=0,
            module_added=0,
            module_port_added=0,
            images=0,
            rack_added=0,
        )
        self.url = settings.NETBOX_URL
        self.token = settings.NETBOX_TOKEN
        self.handle = settings.handle
        self.netbox = None
        self.ignore_ssl = settings.IGNORE_SSL_ERRORS
        self.modules = False
        self.new_filters = True
        self.module_profiles_enabled = False
        self.rack_types_enabled = False
        self.front_port_mappings_enabled = False
        self.connect_api()
        self.verify_compatibility()
        self.existing_manufacturers, self.manufacturers_by_name = self.get_manufacturers()
        self.device_types = DeviceTypes(
            self.netbox, self.handle, self.counter, self.ignore_ssl,
            self.new_filters, self.front_port_mappings_enabled)

    def connect_api(self):
        try:
            self.netbox = pynetbox.api(self.url, token=self.token)
            if self.ignore_ssl:
                self.handle.verbose_log("IGNORE_SSL_ERRORS is True, disabling SSL verification.")
                self.netbox.http_session.verify = False
        except Exception as e:
            self.handle.exception("Exception", 'NetBox API Error', e)

    def get_api(self):
        return self.netbox

    def get_counter(self):
        return self.counter

    def verify_compatibility(self):
        version = self.netbox.version
        parts = [int(x) for x in version.split('.')]
        major = parts[0]
        minor = parts[1] if len(parts) > 1 else 0
        ver = (major, minor)

        if ver < (4, 1):
            self.handle.exception(
                "VersionError", version,
                f"NetBox {version} is not supported. Requires >= 4.1.")

        self.modules = True
        self.new_filters = True
        self.module_profiles_enabled = ver >= (4, 3)
        self.rack_types_enabled = ver >= (4, 2)
        self.front_port_mappings_enabled = ver >= (4, 5)
        self.handle.log(
            f"NetBox {version} detected — "
            f"module profiles: {self.module_profiles_enabled}, "
            f"rack types: {self.rack_types_enabled}, "
            f"front port mappings: {self.front_port_mappings_enabled}.")

    def get_manufacturers(self):
        manufacturers = {}
        by_name = {}
        for mfr in self.netbox.dcim.manufacturers.all():
            manufacturers[str(mfr.slug)] = mfr
            by_name[str(mfr.name).casefold()] = mfr
        return manufacturers, by_name

    def create_manufacturers(self, manufacturers):
        """Ensure all given manufacturers exist, creating missing ones.

        Accepts a list of {'name': str, 'slug': str} dicts.  Looks up by slug
        first, then by case-insensitive name, then creates.  Batch creation
        falls back to one-by-one on failure so a single bad entry does not
        block the rest.
        """
        # Deduplicate by slug while preserving order.
        seen = {}
        for mfr in manufacturers:
            seen[mfr['slug']] = mfr

        to_create = []
        for slug, mfr in seen.items():
            if slug in self.existing_manufacturers:
                continue
            name_cf = str(mfr['name']).casefold()
            if name_cf in self.manufacturers_by_name:
                self.existing_manufacturers[slug] = \
                    self.manufacturers_by_name[name_cf]
                continue
            to_create.append(mfr)

        if not to_create:
            return

        try:
            created = self.netbox.dcim.manufacturers.create(to_create)
            if not isinstance(created, list):
                created = [created]
            for mfr in created:
                slug = str(mfr.slug)
                self.existing_manufacturers[slug] = mfr
                self.manufacturers_by_name[str(mfr.name).casefold()] = mfr
                self.counter.update({'manufacturer': 1})
                self.handle.verbose_log(
                    f'Manufacturer Created: {mfr.name} - {mfr.id}')
        except pynetbox.RequestError as e:
            self.handle.log(
                f"Batch manufacturer creation failed ({e.error}); "
                "retrying individually…")
            for mfr in to_create:
                try:
                    created = self.netbox.dcim.manufacturers.create(mfr)
                    slug = str(getattr(created, 'slug', mfr.get('slug', '')))
                    self.existing_manufacturers[slug] = created
                    self.manufacturers_by_name[
                        str(created.name).casefold()] = created
                    self.counter.update({'manufacturer': 1})
                    self.handle.verbose_log(
                        f'Manufacturer Created: {created.name} - {created.id}')
                except pynetbox.RequestError as e2:
                    self.handle.log(
                        f"Error '{e2.error}' creating manufacturer: "
                        f"{mfr.get('name', mfr)}")

    def _normalize_manufacturer(self, entry):
        """Resolve a device/module/rack type's manufacturer reference by slug.

        The YAML ``manufacturer`` field casing can differ from the directory
        name (e.g. "Unipi technology" vs "Unipi Technology").  NetBox matches on
        both name and slug, so a case-only mismatch fails.  We look up the
        stored manufacturer by slug and replace the reference with its numeric
        ID, which is unambiguous.  Returns the canonical slug.
        """
        mfr = entry.get('manufacturer')
        if not isinstance(mfr, dict):
            return mfr
        slug = mfr.get('slug')
        existing = self.existing_manufacturers.get(slug)
        if existing is not None:
            entry['manufacturer'] = getattr(existing, 'id', existing)
        return slug

    def ensure_module_type_profiles(self, profile_names):
        if not self.module_profiles_enabled or not profile_names:
            return
        try:
            existing = {str(p.name): p
                        for p in self.netbox.dcim.module_type_profiles.all()}
        except pynetbox.RequestError as e:
            self.handle.log(f"Error fetching module type profiles: {e.error}")
            return
        for name in profile_names:
            if name not in existing:
                try:
                    p = self.netbox.dcim.module_type_profiles.create(
                        {'name': name})
                    existing[str(p.name)] = p
                    self.handle.verbose_log(
                        f'Module Type Profile Created: {name}')
                except pynetbox.RequestError as e:
                    self.handle.log(
                        f"Error creating module type profile {name}: {e.error}")

    def create_device_types(self, device_types_to_add):
        for device_type in device_types_to_add:
            src_file = device_type.pop("src")

            # Extract port-mappings (not a device-type API field)
            port_mappings = device_type.pop("port-mappings", None)
            # inventory-items is not currently supported by the importer
            device_type.pop("inventory-items", None)

            # Pre-process front/rear_image flag, remove it if present
            saved_images = {}
            image_base = os.path.dirname(src_file).replace(
                "device-types", "elevation-images")
            for i in ("front_image", "rear_image"):
                if i in device_type:
                    if device_type[i]:
                        image_glob = (
                            f"{image_base}/{device_type['slug']}"
                            f".{i.split('_')[0]}.*")
                        images = glob.glob(image_glob, recursive=False)
                        if images:
                            saved_images[i] = images[0]
                        else:
                            self.handle.log(
                                f"Error locating image file using "
                                f"'{image_glob}'")
                    del device_type[i]

            mfr_slug = self._normalize_manufacturer(device_type)
            model = device_type['model']
            cache_key = (mfr_slug, model)

            try:
                dt = self.device_types.existing_device_types[cache_key]
                self.handle.verbose_log(
                    f'Device Type Exists: {dt.manufacturer.name} - '
                    f'{dt.model} - {dt.id}')
            except KeyError:
                try:
                    dt = self.netbox.dcim.device_types.create(device_type)
                    self.device_types.existing_device_types[cache_key] = dt
                    self.counter.update({'added': 1})
                    self.handle.verbose_log(
                        f'Device Type Created: {dt.manufacturer.name} - '
                        f'{dt.model} - {dt.id}')
                except pynetbox.RequestError as e:
                    self.handle.log(
                        f'Error {e.error} creating device type: '
                        f'{mfr_slug} {model}')
                    continue

            if "interfaces" in device_type:
                self.device_types.create_interfaces(
                    device_type["interfaces"], dt.id)
            if "power-ports" in device_type:
                self.device_types.create_power_ports(
                    device_type["power-ports"], dt.id)
            if "power-port" in device_type:
                self.device_types.create_power_ports(
                    device_type["power-port"], dt.id)
            if "console-ports" in device_type:
                self.device_types.create_console_ports(
                    device_type["console-ports"], dt.id)
            if "power-outlets" in device_type:
                self.device_types.create_power_outlets(
                    device_type["power-outlets"], dt.id)
            if "console-server-ports" in device_type:
                self.device_types.create_console_server_ports(
                    device_type["console-server-ports"], dt.id)
            if "rear-ports" in device_type:
                self.device_types.create_rear_ports(
                    device_type["rear-ports"], dt.id)
            if "front-ports" in device_type:
                self.device_types.create_front_ports(
                    device_type["front-ports"], dt.id, port_mappings)
            if "device-bays" in device_type:
                self.device_types.create_device_bays(
                    device_type["device-bays"], dt.id)
            if self.modules and 'module-bays' in device_type:
                self.device_types.create_module_bays(
                    device_type['module-bays'], dt.id)

            if saved_images:
                try:
                    self.device_types.upload_images(
                        self.url, self.token, saved_images, dt.id)
                except Exception as exc:
                    self.handle.log(
                        f'Image upload skipped for device-type {dt.id}: {exc}')

    def create_module_types(self, module_types):
        all_module_types = {}
        for curr_nb_mt in self.netbox.dcim.module_types.all():
            if curr_nb_mt.manufacturer.slug not in all_module_types:
                all_module_types[curr_nb_mt.manufacturer.slug] = {}
            all_module_types[curr_nb_mt.manufacturer.slug][
                curr_nb_mt.model] = curr_nb_mt

        # Ensure any module type profiles referenced exist (NetBox >= 4.3)
        if self.module_profiles_enabled:
            profile_names = set()
            for mt in module_types:
                p = mt.get('profile')
                if isinstance(p, str) and p:
                    profile_names.add(p)
            if profile_names:
                self.ensure_module_type_profiles(profile_names)

        for curr_mt in module_types:
            port_mappings = curr_mt.get('port-mappings')

            # Build clean payload for the module-type create endpoint
            payload = {
                k: v for k, v in curr_mt.items()
                if k not in MODULE_COMPONENT_KEYS
            }
            if self.module_profiles_enabled:
                if 'profile' in payload and isinstance(payload['profile'], str):
                    payload['profile'] = {'name': payload['profile']}
                if 'attribute_data' in payload:
                    payload['attributes'] = payload.pop('attribute_data')
            else:
                payload.pop('profile', None)
                payload.pop('attribute_data', None)

            mfr_slug = self._normalize_manufacturer(payload)
            model = payload['model']

            try:
                module_type_res = all_module_types[mfr_slug][model]
                self.handle.verbose_log(
                    f'Module Type Exists: '
                    f'{module_type_res.manufacturer.name} - '
                    f'{module_type_res.model} - {module_type_res.id}')
                # Backfill profile/attributes on existing module types
                if self.module_profiles_enabled:
                    updates = {}
                    if (isinstance(payload.get('profile'), dict)
                            and not getattr(module_type_res, 'profile', None)):
                        updates['profile'] = payload['profile']
                    if payload.get('attributes') and not getattr(
                            module_type_res, 'attributes', None):
                        updates['attributes'] = payload['attributes']
                    if updates:
                        try:
                            module_type_res.update(updates)
                            self.handle.verbose_log(
                                f'Updated module type profile/attributes: '
                                f'{module_type_res.manufacturer.name} '
                                f'{module_type_res.model}')
                        except Exception as exc:
                            self.handle.log(
                                f'Error updating module type '
                                f'{module_type_res.id}: {exc}')
            except KeyError:
                try:
                    module_type_res = self.netbox.dcim.module_types.create(
                        payload)
                    self.counter.update({'module_added': 1})
                    self.handle.verbose_log(
                        f'Module Type Created: '
                        f'{module_type_res.manufacturer.name} - '
                        f'{module_type_res.model} - '
                        f'{module_type_res.id}')
                except pynetbox.RequestError as exce:
                    self.handle.log(
                        f"Error '{exce.error}' creating module type: "
                        f"{payload.get('manufacturer')}, {model}")
                    continue

            if "interfaces" in curr_mt:
                self.device_types.create_module_interfaces(
                    curr_mt["interfaces"], module_type_res.id)
            if "power-ports" in curr_mt:
                self.device_types.create_module_power_ports(
                    curr_mt["power-ports"], module_type_res.id)
            if "console-ports" in curr_mt:
                self.device_types.create_module_console_ports(
                    curr_mt["console-ports"], module_type_res.id)
            if "power-outlets" in curr_mt:
                self.device_types.create_module_power_outlets(
                    curr_mt["power-outlets"], module_type_res.id)
            if "console-server-ports" in curr_mt:
                self.device_types.create_module_console_server_ports(
                    curr_mt["console-server-ports"], module_type_res.id)
            if "rear-ports" in curr_mt:
                self.device_types.create_module_rear_ports(
                    curr_mt["rear-ports"], module_type_res.id)
            if "front-ports" in curr_mt:
                self.device_types.create_module_front_ports(
                    curr_mt["front-ports"], module_type_res.id, port_mappings)
            if "module-bays" in curr_mt:
                self.device_types.create_module_module_bays(
                    curr_mt["module-bays"], module_type_res.id)

    def create_rack_types(self, rack_types):
        if not self.rack_types_enabled:
            self.handle.log(
                "Rack types not supported on this NetBox version, skipping.")
            return

        existing = {}
        for rt in self.netbox.dcim.rack_types.all():
            key = (str(rt.manufacturer.slug), str(rt.model))
            existing[key] = rt

        for rack_type in rack_types:
            rack_type.pop('src', None)
            mfr_slug = self._normalize_manufacturer(rack_type)
            model = rack_type['model']
            cache_key = (mfr_slug, model)

            if cache_key in existing:
                self.handle.verbose_log(
                    f'Rack Type Exists: {model} - {existing[cache_key].id}')
                continue

            try:
                rt = self.netbox.dcim.rack_types.create(rack_type)
                existing[cache_key] = rt
                self.counter.update({'rack_added': 1})
                self.handle.verbose_log(
                    f'Rack Type Created: {rt.manufacturer.name} - '
                    f'{rt.model} - {rt.id}')
            except pynetbox.RequestError as e:
                self.handle.log(
                    f'Error {e.error} creating rack type: {mfr_slug} {model}')


class DeviceTypes:
    def __new__(cls, *args, **kwargs):
        return super().__new__(cls)

    def __init__(self, netbox, handle, counter, ignore_ssl, new_filters,
                 front_port_mappings_enabled):
        self.netbox = netbox
        self.handle = handle
        self.counter = counter
        self.existing_device_types = self.get_device_types()
        self.ignore_ssl = ignore_ssl
        self.new_filters = new_filters
        self.front_port_mappings_enabled = front_port_mappings_enabled

    def get_device_types(self):
        return {
            (str(item.manufacturer.slug), str(item.model)): item
            for item in self.netbox.dcim.device_types.all()
        }

    def get_power_ports(self, device_type):
        return {str(item): item for item in self.netbox.dcim.power_port_templates.filter(
            **{'device_type_id' if self.new_filters else 'devicetype_id': device_type})}

    def get_rear_ports(self, device_type):
        return {str(item): item for item in self.netbox.dcim.rear_port_templates.filter(
            **{'device_type_id' if self.new_filters else 'devicetype_id': device_type})}

    def get_module_power_ports(self, module_type):
        return {str(item): item for item in self.netbox.dcim.power_port_templates.filter(
            **{'module_type_id' if self.new_filters else 'moduletype_id': module_type})}

    def get_module_rear_ports(self, module_type):
        return {str(item): item for item in self.netbox.dcim.rear_port_templates.filter(
            **{'module_type_id' if self.new_filters else 'moduletype_id': module_type})}

    def get_device_type_ports_to_create(self, dcim_ports, device_type,
                                        existing_ports):
        to_create = [port for port in dcim_ports
                     if port['name'] not in existing_ports]
        for port in to_create:
            port['device_type'] = device_type
        return to_create

    def get_module_type_ports_to_create(self, module_ports, module_type,
                                         existing_ports):
        to_create = [port for port in module_ports
                     if port['name'] not in existing_ports]
        for port in to_create:
            port['module_type'] = module_type
        return to_create

    def _set_interface_bridges(self, created, existing, bridge_refs):
        """Resolve the self-referential ``bridge`` field on interface templates.

        The YAML expresses ``bridge`` as the *name* of another interface in the
        same device/module type (e.g. ``backplane0``).  NetBox requires either a
        numeric ID or an attribute dict, so after interfaces are created we
        build a name → interface mapping and update each referencing interface
        with the resolved ID.
        """
        if not bridge_refs:
            return

        name_to_iface = {str(item): item for item in
                         (created if isinstance(created, list) else [created])}
        name_to_iface.update(existing)

        for name, bridge_name in bridge_refs.items():
            iface = name_to_iface.get(name)
            target = name_to_iface.get(bridge_name)
            if not iface or not target:
                self.handle.log(
                    f'Could not resolve bridge for interface {name}: '
                    f'{bridge_name}')
                continue
            try:
                iface.update({'bridge': target.id})
                self.handle.verbose_log(
                    f'Bridge set: {name} -> {bridge_name} ({target.id})')
            except pynetbox.RequestError as excep:
                self.handle.log(
                    f"Error '{excep.error}' setting bridge on interface {name}")

    # ------------------------------------------------------------------ #
    # Front port rear-mapping resolution (NetBox >= 4.5 vs < 4.5)
    # ------------------------------------------------------------------ #
    def _resolve_front_port_rear_mappings(self, ports, rear_ports,
                                          port_mappings):
        """Resolve rear-port references in front-port payloads.

        NetBox 4.5+ uses ``rear_ports`` (array of mappings with position,
        rear_port ID, rear_port_position).  Earlier versions use a singular
        ``rear_port`` FK + ``rear_port_position``.

        Modifies *ports* in-place and returns a list of ports that could not
        be resolved (to be skipped).
        """
        if not ports:
            return []

        mapping_lookup = {}
        if port_mappings:
            for m in port_mappings:
                fp = m['front_port']
                mapping_lookup.setdefault(fp, []).append((
                    m.get('front_port_position', 1),
                    m['rear_port'],
                    m.get('rear_port_position', 1),
                ))

        skipped = []
        for port in ports:
            name = port['name']
            mappings = mapping_lookup.get(name, [])

            if self.front_port_mappings_enabled:
                # NetBox >= 4.5: rear_ports array of {position, rear_port,
                # rear_port_position}
                rear_ports_list = []
                for (fp_pos, rp_name, rp_pos) in mappings:
                    rp = rear_ports.get(rp_name)
                    if rp:
                        rear_ports_list.append({
                            'position': fp_pos,
                            'rear_port': rp.id,
                            'rear_port_position': rp_pos,
                        })
                    else:
                        self.handle.log(
                            f'Could not find Rear Port for Front Port: '
                            f'{name} - {rp_name}')
                # Fallback to inline rear_port (old-style, rare)
                if not rear_ports_list and 'rear_port' in port:
                    rp = rear_ports.get(port['rear_port'])
                    if rp:
                        rear_ports_list.append({
                            'position': 1,
                            'rear_port': rp.id,
                            'rear_port_position': port.get(
                                'rear_port_position', 1),
                        })
                    else:
                        self.handle.log(
                            f'Could not find Rear Port for Front Port: '
                            f'{name} - {port["rear_port"]}')
                if rear_ports_list:
                    port['rear_ports'] = rear_ports_list
                else:
                    self.handle.log(
                        f'No rear port mapping for Front Port: {name}')
                    skipped.append(port)
                port.pop('rear_port', None)
                port.pop('rear_port_position', None)
            else:
                # NetBox 4.1-4.4: rear_port (singular) + rear_port_position
                if mappings:
                    _, rp_name, rp_pos = mappings[0]
                    rp = rear_ports.get(rp_name)
                    if rp:
                        port['rear_port'] = rp.id
                        port['rear_port_position'] = rp_pos
                    else:
                        self.handle.log(
                            f'Could not find Rear Port for Front Port: '
                            f'{name} - {rp_name}')
                        skipped.append(port)
                elif 'rear_port' in port:
                    rp = rear_ports.get(port['rear_port'])
                    if rp:
                        port['rear_port'] = rp.id
                    else:
                        self.handle.log(
                            f'Could not find Rear Port for Front Port: '
                            f'{name} - {port["rear_port"]}')
                        skipped.append(port)
                else:
                    self.handle.log(
                        f'No rear port mapping for Front Port: {name}')
                    skipped.append(port)

        return skipped

    # ------------------------------------------------------------------ #
    # Device-type component templates
    # ------------------------------------------------------------------ #
    def create_interfaces(self, interfaces, device_type):
        existing = {str(item): item for item in self.netbox.dcim.interface_templates.filter(
            **{'device_type_id' if self.new_filters else 'devicetype_id': device_type})}
        to_create = self.get_device_type_ports_to_create(
            interfaces, device_type, existing)
        bridge_refs = {port['name']: port['bridge'] for port in to_create
                       if port.get('bridge')}
        for port in to_create:
            port.pop('bridge', None)
        if to_create:
            try:
                created = self.netbox.dcim.interface_templates.create(to_create)
                self.counter.update({'updated':
                    self.handle.log_device_ports_created(
                        created, "Interface")})
                self._set_interface_bridges(
                    created, existing, bridge_refs)
            except pynetbox.RequestError as excep:
                self.handle.log(f"Error '{excep.error}' creating Interface")

    def create_power_ports(self, power_ports, device_type):
        existing_power_ports = self.get_power_ports(device_type)
        to_create = self.get_device_type_ports_to_create(
            power_ports, device_type, existing_power_ports)
        if to_create:
            try:
                self.counter.update({'updated':
                    self.handle.log_device_ports_created(
                        self.netbox.dcim.power_port_templates.create(to_create),
                        "Power Port")})
            except pynetbox.RequestError as excep:
                self.handle.log(f"Error '{excep.error}' creating Power Port")

    def create_console_ports(self, console_ports, device_type):
        existing = {str(item): item for item in self.netbox.dcim.console_port_templates.filter(
            **{'device_type_id' if self.new_filters else 'devicetype_id': device_type})}
        to_create = self.get_device_type_ports_to_create(
            console_ports, device_type, existing)
        if to_create:
            try:
                self.counter.update({'updated':
                    self.handle.log_device_ports_created(
                        self.netbox.dcim.console_port_templates.create(to_create),
                        "Console Port")})
            except pynetbox.RequestError as excep:
                self.handle.log(f"Error '{excep.error}' creating Console Port")

    def create_power_outlets(self, power_outlets, device_type):
        existing = {str(item): item for item in self.netbox.dcim.power_outlet_templates.filter(
            **{'device_type_id' if self.new_filters else 'devicetype_id': device_type})}
        to_create = self.get_device_type_ports_to_create(
            power_outlets, device_type, existing)
        if to_create:
            existing_power_ports = self.get_power_ports(device_type)
            for outlet in to_create:
                try:
                    power_port = existing_power_ports[outlet["power_port"]]
                    outlet['power_port'] = power_port.id
                except KeyError:
                    pass
            try:
                self.counter.update({'updated':
                    self.handle.log_device_ports_created(
                        self.netbox.dcim.power_outlet_templates.create(to_create),
                        "Power Outlet")})
            except pynetbox.RequestError as excep:
                self.handle.log(f"Error '{excep.error}' creating Power Outlet")

    def create_console_server_ports(self, console_server_ports, device_type):
        existing = {str(item): item for item in self.netbox.dcim.console_server_port_templates.filter(
            **{'device_type_id' if self.new_filters else 'devicetype_id': device_type})}
        to_create = self.get_device_type_ports_to_create(
            console_server_ports, device_type, existing)
        if to_create:
            try:
                self.counter.update({'updated':
                    self.handle.log_device_ports_created(
                        self.netbox.dcim.console_server_port_templates.create(to_create),
                        "Console Server Port")})
            except pynetbox.RequestError as excep:
                self.handle.log(
                    f"Error '{excep.error}' creating Console Server Port")

    def create_rear_ports(self, rear_ports, device_type):
        existing_rear_ports = self.get_rear_ports(device_type)
        to_create = self.get_device_type_ports_to_create(
            rear_ports, device_type, existing_rear_ports)
        if to_create:
            try:
                self.counter.update({'updated':
                    self.handle.log_device_ports_created(
                        self.netbox.dcim.rear_port_templates.create(to_create),
                        "Rear Port")})
            except pynetbox.RequestError as excep:
                self.handle.log(f"Error '{excep.error}' creating Rear Port")

    def create_front_ports(self, front_ports, device_type, port_mappings=None):
        existing_front_ports = {str(item): item for item in self.netbox.dcim.front_port_templates.filter(
            **{'device_type_id' if self.new_filters else 'devicetype_id': device_type})}
        to_create = self.get_device_type_ports_to_create(
            front_ports, device_type, existing_front_ports)
        if not to_create:
            return

        all_rearports = self.get_rear_ports(device_type)
        skipped = self._resolve_front_port_rear_mappings(
            to_create, all_rearports, port_mappings)
        for p in skipped:
            to_create.remove(p)

        if to_create:
            try:
                self.counter.update({'updated':
                    self.handle.log_device_ports_created(
                        self.netbox.dcim.front_port_templates.create(to_create),
                        "Front Port")})
            except pynetbox.RequestError as excep:
                self.handle.log(f"Error '{excep.error}' creating Front Port")

    def create_device_bays(self, device_bays, device_type):
        existing = {str(item): item for item in self.netbox.dcim.device_bay_templates.filter(
            **{'device_type_id' if self.new_filters else 'devicetype_id': device_type})}
        to_create = self.get_device_type_ports_to_create(
            device_bays, device_type, existing)
        if to_create:
            try:
                self.counter.update({'updated':
                    self.handle.log_device_ports_created(
                        self.netbox.dcim.device_bay_templates.create(to_create),
                        "Device Bay")})
            except pynetbox.RequestError as excep:
                self.handle.log(f"Error '{excep.error}' creating Device Bay")

    def create_module_bays(self, module_bays, device_type):
        existing = {str(item): item for item in self.netbox.dcim.module_bay_templates.filter(
            **{'device_type_id' if self.new_filters else 'devicetype_id': device_type})}
        to_create = self.get_device_type_ports_to_create(
            module_bays, device_type, existing)
        if to_create:
            try:
                self.counter.update({'updated':
                    self.handle.log_device_ports_created(
                        self.netbox.dcim.module_bay_templates.create(to_create),
                        "Module Bay")})
            except pynetbox.RequestError as excep:
                self.handle.log(f"Error '{excep.error}' creating Module Bay")

    # ------------------------------------------------------------------ #
    # Module-type component templates
    # ------------------------------------------------------------------ #
    def create_module_interfaces(self, module_interfaces, module_type):
        existing = {str(item): item for item in self.netbox.dcim.interface_templates.filter(
            **{'module_type_id' if self.new_filters else 'moduletype_id': module_type})}
        to_create = self.get_module_type_ports_to_create(
            module_interfaces, module_type, existing)
        bridge_refs = {port['name']: port['bridge'] for port in to_create
                       if port.get('bridge')}
        for port in to_create:
            port.pop('bridge', None)
        if to_create:
            try:
                created = self.netbox.dcim.interface_templates.create(to_create)
                self.counter.update({'updated':
                    self.handle.log_module_ports_created(
                        created, "Module Interface")})
                self._set_interface_bridges(
                    created, existing, bridge_refs)
            except pynetbox.RequestError as excep:
                self.handle.log(
                    f"Error '{excep.error}' creating Module Interface")

    def create_module_power_ports(self, power_ports, module_type):
        existing = self.get_module_power_ports(module_type)
        to_create = self.get_module_type_ports_to_create(
            power_ports, module_type, existing)
        if to_create:
            try:
                self.counter.update({'updated':
                    self.handle.log_module_ports_created(
                        self.netbox.dcim.power_port_templates.create(to_create),
                        "Module Power Port")})
            except pynetbox.RequestError as excep:
                self.handle.log(
                    f"Error '{excep.error}' creating Module Power Port")

    def create_module_console_ports(self, console_ports, module_type):
        existing = {str(item): item for item in self.netbox.dcim.console_port_templates.filter(
            **{'module_type_id' if self.new_filters else 'moduletype_id': module_type})}
        to_create = self.get_module_type_ports_to_create(
            console_ports, module_type, existing)
        if to_create:
            try:
                self.counter.update({'updated':
                    self.handle.log_module_ports_created(
                        self.netbox.dcim.console_port_templates.create(to_create),
                        "Module Console Port")})
            except pynetbox.RequestError as excep:
                self.handle.log(
                    f"Error '{excep.error}' creating Module Console Port")

    def create_module_power_outlets(self, power_outlets, module_type):
        existing = {str(item): item for item in self.netbox.dcim.power_outlet_templates.filter(
            **{'module_type_id' if self.new_filters else 'moduletype_id': module_type})}
        to_create = self.get_module_type_ports_to_create(
            power_outlets, module_type, existing)
        if to_create:
            existing_power_ports = self.get_module_power_ports(module_type)
            for outlet in to_create:
                try:
                    power_port = existing_power_ports[outlet["power_port"]]
                    outlet['power_port'] = power_port.id
                except KeyError:
                    pass
            try:
                self.counter.update({'updated':
                    self.handle.log_module_ports_created(
                        self.netbox.dcim.power_outlet_templates.create(to_create),
                        "Module Power Outlet")})
            except pynetbox.RequestError as excep:
                self.handle.log(
                    f"Error '{excep.error}' creating Module Power Outlet")

    def create_module_console_server_ports(self, console_server_ports, module_type):
        existing = {str(item): item for item in self.netbox.dcim.console_server_port_templates.filter(
            **{'module_type_id' if self.new_filters else 'moduletype_id': module_type})}
        to_create = self.get_module_type_ports_to_create(
            console_server_ports, module_type, existing)
        if to_create:
            try:
                self.counter.update({'updated':
                    self.handle.log_module_ports_created(
                        self.netbox.dcim.console_server_port_templates.create(to_create),
                        "Module Console Server Port")})
            except pynetbox.RequestError as excep:
                self.handle.log(
                    f"Error '{excep.error}' creating Module Console Server Port")

    def create_module_rear_ports(self, rear_ports, module_type):
        existing_rear_ports = self.get_module_rear_ports(module_type)
        to_create = self.get_module_type_ports_to_create(
            rear_ports, module_type, existing_rear_ports)
        if to_create:
            try:
                self.counter.update({'updated':
                    self.handle.log_module_ports_created(
                        self.netbox.dcim.rear_port_templates.create(to_create),
                        "Module Rear Port")})
            except pynetbox.RequestError as excep:
                self.handle.log(
                    f"Error '{excep.error}' creating Module Rear Port")

    def create_module_front_ports(self, front_ports, module_type,
                                   port_mappings=None):
        existing_front_ports = {str(item): item for item in self.netbox.dcim.front_port_templates.filter(
            **{'module_type_id' if self.new_filters else 'moduletype_id': module_type})}
        to_create = self.get_module_type_ports_to_create(
            front_ports, module_type, existing_front_ports)
        if not to_create:
            return

        all_rear_ports = self.get_module_rear_ports(module_type)
        skipped = self._resolve_front_port_rear_mappings(
            to_create, all_rear_ports, port_mappings)
        for p in skipped:
            to_create.remove(p)

        if to_create:
            try:
                self.counter.update({'updated':
                    self.handle.log_module_ports_created(
                        self.netbox.dcim.front_port_templates.create(to_create),
                        "Module Front Port")})
            except pynetbox.RequestError as excep:
                self.handle.log(
                    f"Error '{excep.error}' creating Module Front Port")

    def create_module_module_bays(self, module_bays, module_type):
        existing = {str(item): item for item in self.netbox.dcim.module_bay_templates.filter(
            **{'module_type_id' if self.new_filters else 'moduletype_id': module_type})}
        to_create = self.get_module_type_ports_to_create(
            module_bays, module_type, existing)
        if to_create:
            try:
                self.counter.update({'updated':
                    self.handle.log_module_ports_created(
                        self.netbox.dcim.module_bay_templates.create(to_create),
                        "Module Bay")})
            except pynetbox.RequestError as excep:
                self.handle.log(f"Error '{excep.error}' creating Module Bay")

    # ------------------------------------------------------------------ #
    # Image upload
    # ------------------------------------------------------------------ #
    def upload_images(self, baseurl, token, images, device_type):
        """Upload front_image and/or rear_image for the given device type."""
        url = f"{baseurl.rstrip('/')}/api/dcim/device-types/{device_type}/"
        headers = {"Authorization": f"Token {token}"}

        files = {}
        for i, f in images.items():
            files[i] = (os.path.basename(f), open(f, "rb"))
        try:
            response = requests.patch(
                url, headers=headers, files=files,
                verify=(not self.ignore_ssl), timeout=60)
            if response.status_code >= 400:
                self.handle.log(
                    f'Image upload failed for device-type {device_type}: '
                    f'HTTP {response.status_code} {response.text[:200]}')
            else:
                self.handle.verbose_log(
                    f'Images updated for device-type {device_type}: '
                    f'{list(images.keys())}')
                self.counter["images"] += len(images)
        except requests.RequestException as exc:
            self.handle.log(
                f'Image upload error for device-type {device_type}: {exc}')
        finally:
            for _, fh in files.values():
                fh.close()
