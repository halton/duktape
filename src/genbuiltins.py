#!/usr/bin/env python2
#
#  Generate initialization data for built-in strings and objects.
#
#  Supports two different initialization approaches:
#
#    1. Bit-packed format for unpacking strings and objects during
#       heap or thread init into RAM-based structures.  This is the
#       default behavior.
#
#    2. Embedding strings and/or objects into a read-only data section
#       at compile time.  This is useful for low memory targets to reduce
#       memory usage.  Objects in data section will be immutable.
#
#  Both of these have practical complications like endianness differences,
#  pointer compression variants, object property table layout variants,
#  and so on.  Multiple #ifdef'd initializer sections are emitted to cover
#  all supported alternatives.
#

import os
import sys
import re
import json
import yaml
import math
import struct
import optparse
import copy

import dukutil

# Fixed seed for ROM strings, must match src/duk_heap_alloc.c.
DUK__FIXED_HASH_SEED = 0xabcd1234

# Base value for compressed ROM pointers, used range is [ROMPTR_FIRST,0xffff].
# Must match DUK_USE_ROM_PTRCOMP_FIRST (generated header checks).
ROMPTR_FIRST = 0xf800  # 2048 should be enough; now around ~1000 used

#
#  Miscellaneous helpers
#

# Convert Unicode to bytes, identifying Unicode U+0000 to U+00FF as bytes.
# This representation is used in YAML metadata and allows invalid UTF-8 to
# be represented exactly (which is necessary).
def unicode_to_bytes(x):
	if isinstance(x, str):
		return x
	tmp = ''
	for c in x:
		if ord(c) > 0xff:
			raise Exception('invalid codepoint: %r' % x)
		tmp += chr(ord(c))
	assert(isinstance(tmp, str))
	return tmp

# Convert bytes to Unicode, identifying bytes as U+0000 to U+00FF.
def bytes_to_unicode(x):
	if isinstance(x, unicode):
		return x
	tmp = u''
	for c in x:
		tmp += unichr(ord(c))
	assert(isinstance(tmp, unicode))
	return tmp

# Convert all strings in an object to bytes recursively.  Useful for
# normalizing all strings in a YAML document.
def recursive_strings_to_bytes(doc):
	def f(x):
		if isinstance(x, unicode):
			return unicode_to_bytes(x)
		if isinstance(x, dict):
			res = {}
			for k in x.keys():
				res[f(k)] = f(x[k])
			return res
		if isinstance(x, list):
			res = []
			for e in x:
				res.append(f(e))
			return res
		return x

	return f(doc)

# Convert all strings in an object to from bytes to Unicode recursively.
# Useful for writing back JSON/YAML dumps.
def recursive_bytes_to_strings(doc):
	def f(x):
		if isinstance(x, str):
			return bytes_to_unicode(x)
		if isinstance(x, dict):
			res = {}
			for k in x.keys():
				res[f(k)] = f(x[k])
			return res
		if isinstance(x, list):
			res = []
			for e in x:
				res.append(f(e))
			return res
		return x

	return f(doc)

# Check if string is an "array index" in Ecmascript terms.
def string_is_arridx(v):
	is_arridx = False
	try:
		ival = int(v)
		if ival >= 0 and ival <= 0xfffffffe and ('%d' % ival == v):
			is_arridx = True
	except ValueError:
		pass

	return is_arridx

#
#  Metadata loading, merging, and other preprocessing
#
#  Final metadata object contains merged and normalized objects and strings.
#  Keys added include (see more below):
#
#    strings_stridx: string objects which have a stridx, matches stridx index order
#    objects_bidx: objects which have a bidx, matches bidx index order
#    objects_ram_toplevel: objects which are top level for RAM init
#
#  Various helper keys are also added, containing auxiliary object/string
#  lists, lookup maps, etc.  See code below for details of these.
#

def metadata_lookup_object(meta, obj_id):
	return meta['_objid_to_object'][obj_id]

def metadata_lookup_property(obj, key):
	for p in obj['properties']:
		if p['key'] == key:
			return p
	raise Exception('cannot find property %s from object %s' % (key, obj_id))

# Remove disabled objects and properties.
def metadata_remove_disabled(meta):
	objlist = []
	for o in meta['objects']:
		if o.get('disable', False):
			print('Remove disabled object: %s' % o['id'])
		else:
			objlist.append(o)

		props = []
		for p in o['properties']:
			if p.get('disable', False):
				print('Remove disabled property: %s, object: %s' % (p['key'], o['id']))
			else:
				props.append(p)

		o['properties'] = props

	meta['objects'] = objlist

# Delete dangling references to removed/missing objects.
def metadata_delete_dangling_references_to_object(meta, obj_id):
	for o in meta['objects']:
		new_p = []
		for p in o['properties']:
			v = p['value']
			ptype = None
			if isinstance(v, dict):
				ptype = p['value']['type']
			delprop = False
			if ptype == 'object' and v['id'] == obj_id:
				delprop = True
			if ptype == 'accessor' and v.get('getter_id') == obj_id:
				p['getter_id'] = None
			if ptype == 'accessor' and v.get('setter_id') == obj_id:
				p['setter_id'] = None
			# XXX: Should empty accessor (= no getter, no setter) be deleted?
			# If so, beware of shorthand.
			if delprop:
				print('Deleted property %s of object %s, points to deleted object %s' % \
				      (p['key'], o['id'], obj_id))
			else:
				new_p.append(p)
		o['properties'] = new_p

# Merge a user YAML file into current metadata.
def metadata_merge_user_objects(meta, user_meta):
	# XXX: could be reused from other call sites
	def _findObject(objid):
		for i,t in enumerate(meta['objects']):
			if t['id'] == objid:
				return t, i
		return None, None
	def _findProp(obj, key):
		for i,t in enumerate(obj['properties']):
			if t['key'] == key:
				return t, i
		return None, None

	if user_meta.has_key('add_objects'):
		raise Exception('"add_objects" removed, use "objects" with "add: True"')
	if user_meta.has_key('replace_objects'):
		raise Exception('"replace_objects" removed, use "objects" with "replace: True"')
	if user_meta.has_key('modify_objects'):
		raise Exception('"modify_objects" removed, use "objects" with "modify: True"')

	for o in user_meta.get('objects', []):
		if o.get('disable', False):
			print('Skip disabled object: %s' % o['id'])
			continue
		targ, targ_idx = _findObject(o['id'])

		if o.get('delete', False):
			print('Delete object: %s' % targ['id'])
			if targ is None:
				raise Exception('Cannot delete object %s which doesn\'t exist' % o['id'])
			meta['objects'].pop(targ_idx)
			metadata_delete_dangling_references_to_object(meta, targ['id'])
			continue

		if o.get('replace', False):
			print('Replace object %s' % o['id'])
			if targ is None:
				print('WARNING: object to be replaced doesn\'t exist, append new object')
				meta['objects'].append(o)
			else:
				meta['objects'][targ_idx] = o
			continue

		if o.get('add', False) or not o.get('modify', False):  # 'add' is the default
			print('Add object %s' % o['id'])
			if targ is not None:
				raise Exception('Cannot add object %s which already exists' % o['id'])
			meta['objects'].append(o)
			continue

		assert(o.get('modify', False))  # modify handling
		if targ is None:
			raise Exception('Cannot modify object %s which doesn\'t exist' % o['id'])

		for k in sorted(o.keys()):
			# Merge top level keys by copying over, except 'properties'
			if k == 'properties':
				continue
			targ[k] = o[k]
		for p in o.get('properties', []):
			if p.get('disable', False):
				print('Skip disabled property: %s' % p['key'])
				continue
			prop = None
			prop_idx = None
			prop, prop_idx = _findProp(targ, p['key'])
			if prop is not None:
				if p.get('delete', False):
					print('Delete property %s of %s' % (p['key'], o['id']))
					targ['properties'].pop(prop_idx)
				else:
					print('Replace property %s of %s' % (p['key'], o['id']))
					targ['properties'][prop_idx] = p
			else:
				if p.get('delete', False):
					print('Deleting property %s of %s: doesn\'t exist, nop' % (p['key'], o['id']))
				else:
					print('Add property %s of %s' % (p['key'], o['id']))
					targ['properties'].append(p)

# Normalize nargs for top level functions by defaulting 'nargs' from 'length'.
def metadata_normalize_nargs_length(meta):
	# Default 'nargs' from 'length' for top level function objects.
	for o in meta['objects']:
		if o.has_key('nargs'):
			continue
		if not o.get('callable', False):
			continue
		for p in o['properties']:
			if p['key'] != 'length':
				continue
			#print('Default nargs for top level: %r' % p)
			assert(isinstance(p['value'], int))
			o['nargs'] = p['value']
			break
		assert(o.has_key('nargs'))

	# Default 'nargs' from 'length' for function property shorthand.
	for o in meta['objects']:
		for p in o['properties']:
			if not (isinstance(p['value'], dict) and p['value']['type'] == 'function'):
				continue
			pval = p['value']
			if not pval.has_key('length'):
				print('Default length for function shorthand: %r' % p)
				pval['length'] = 0
			if not pval.has_key('nargs'):
				#print('Default nargs for function shorthand: %r' % p)
				pval['nargs'] = pval['length']

# Prepare a list of built-in objects which need a runtime 'bidx'.
def metadata_prepare_objects_bidx(meta):
	objlist = meta['objects']
	meta['objects'] = []
	meta['objects_bidx'] = []
	objid_map = {}  # temp map

	# Build helper index.
	for o in objlist:
		objid_map[o['id']] = o

	# Use 'builtins' as the bidx list with no filtering for now.
	# Ideally we'd scan the actually needed indices from the source.
	for o in meta['builtins']:
		# No filtering now, just use list as is
		obj = objid_map[o['id']]
		obj['bidx_used'] = True
		meta['objects'].append(obj)
		meta['objects_bidx'].append(obj)

	# Append remaining objects.
	for o in objlist:
		if o.get('bidx_used', False):
			# Already in meta['objects'].
			pass
		else:
			meta['objects'].append(o)

# Normalize metadata property shorthand.  For example, if a proprety value
# is a shorthand function, create a function object and change the property
# to point to that function object.
def metadata_normalize_shorthand(meta):
	# Gather objects through the top level built-ins list.
	objs = []
	subobjs = []

	def getSubObject():
		obj = {}
		obj['id'] = 'subobj_%d' % len(subobjs)  # synthetic ID
		obj['properties'] = []
		obj['auto_generated'] = True  # mark as autogenerated (just FYI)
		subobjs.append(obj)
		return obj

	def decodeFunctionShorthand(funprop):
		# Convert the built-in function property "shorthand" into an actual
		# object for ROM built-ins.
		assert(funprop['value']['type'] == 'function')
		val = funprop['value']
		obj = getSubObject()
		props = obj['properties']
		obj['native'] = val['native']
		obj['nargs'] = val.get('nargs', val['length'])
		obj['varargs'] = val.get('varargs', False)
		obj['magic'] = val.get('magic', 0)
		obj['internal_prototype'] = 'bi_function_prototype'
		obj['class'] = 'Function'
		obj['callable'] = True
		obj['constructable'] = val.get('constructable', False)
		props.append({ 'key': 'length', 'value': val['length'], 'attributes': '' })
		props.append({ 'key': 'name', 'value': funprop['key'], 'attributes': '' })
		return obj

	def addAccessor(funprop, magic, nargs, length, name, native_func):
		assert(funprop['value']['type'] == 'accessor')
		obj = getSubObject()
		props = obj['properties']
		obj['native'] = native_func
		obj['nargs'] = nargs
		obj['varargs'] = False
		obj['magic'] = magic
		obj['internal_prototype'] = 'bi_function_prototype'
		obj['class'] = 'Function'
		obj['callable'] = True
		obj['constructable'] = False
		# Shorthand accessors are minimal and have no .length or .name
		# right now.  Use longhand if these matter.
		#props.append({ 'key': 'length', 'value': length, 'attributes': '' })
		#props.append({ 'key': 'name', 'value': name, 'attributes': '' })
		return obj

	def decodeGetterShorthand(key, funprop):
		assert(funprop['value']['type'] == 'accessor')
		val = funprop['value']
		return addAccessor(funprop,
		                   val['getter_magic'],
		                   val['getter_nargs'],
		                   val.get('getter_length', 0),
		                   key,
		                   val['getter'])

	def decodeSetterShorthand(key, funprop):
		assert(funprop['value']['type'] == 'accessor')
		val = funprop['value']
		return addAccessor(funprop,
		                   val['setter_magic'],
		                   val['setter_nargs'],
		                   val.get('setter_length', 0),
		                   key,
		                   val['setter'])

	def decodeStructuredValue(val):
		#print('Decode structured value: %r' % val)
		if isinstance(val, (int, long, float, str)):
			return val  # as is
		elif isinstance(val, (dict)):
			# Object: decode recursively
			obj = decodeStructuredObject(val)
			return { 'type': 'object', 'id': obj['id'] }
		elif isinstance(val, (list)):
			raise Exception('structured shorthand does not yet support array literals')
		else:
			raise Exception('unsupported value in structured shorthand: %r' % v)

	def decodeStructuredObject(val):
		# XXX: We'd like to preserve dict order from YAML source but
		# Python doesn't do that.  Use sorted order to make the result
		# deterministic.  User can always use longhand for exact
		# property control.

		#print('Decode structured object: %r' % val)
		obj = getSubObject()
		obj['class'] = 'Object'
		obj['internal_prototype'] = 'bi_object_prototype'

		props = obj['properties']
		keys = sorted(val.keys())
		for k in keys:
			#print('Decode property %s' % k)
			prop = { 'key': k, 'value': decodeStructuredValue(val[k]), 'attributes': 'wec' }
			props.append(prop)

		return obj

	def decodeStructuredShorthand(structprop):
		assert(structprop['value']['type'] == 'structured')
		val = structprop['value']['value']
		return decodeStructuredValue(val)

	for idx,obj in enumerate(meta['objects']):
		props = []
		repl_props = []

		for val in obj['properties']:
			# Date.prototype.toGMTString must point to the same Function object
			# as Date.prototype.toUTCString, so special case hack it here.
			if obj['id'] == 'bi_date_prototype' and val['key'] == 'toGMTString':
				#print('Skip Date.prototype.toGMTString')
				continue

			if isinstance(val['value'], dict) and val['value']['type'] == 'function':
				# Function shorthand.
				subfun = decodeFunctionShorthand(val)
				prop = { 'key': val['key'], 'value': { 'type': 'object', 'id': subfun['id'] }, 'attributes': val['attributes'] }
				repl_props.append(prop)
			elif isinstance(val['value'], dict) and val['value']['type'] == 'accessor' and \
			     (val['value'].has_key('getter') or val['value'].has_key('setter')):
				# Accessor normal and shorthand forms both use the type 'accessor',
				# but are differentiated by properties.
				sub_getter = decodeGetterShorthand(val['key'], val)
				sub_setter = decodeSetterShorthand(val['key'], val)
				prop = { 'key': val['key'], 'value': { 'type': 'accessor', 'getter_id': sub_getter['id'], 'setter_id': sub_setter['id'] }, 'attributes': val['attributes'] }
				assert('a' in prop['attributes'])  # If missing, weird things happen runtime
				#print('Expand accessor shorthand: %r -> %r' % (val, prop))
				repl_props.append(prop)
			elif isinstance(val['value'], dict) and val['value']['type'] == 'structured':
				# Structured shorthand.
				subval = decodeStructuredShorthand(val)
				prop = { 'key': val['key'], 'value': subval, 'attributes': val['attributes'] }
				repl_props.append(prop)
				print('Decoded structured shorthand for object %s, property %s' % (obj['id'], val['key']))
			elif isinstance(val['value'], dict) and val['value']['type'] == 'buffer':
				# Duktape buffer type not yet supported.
				raise Exception('Buffer type not yet supported for builtins: %r' % val)
			elif isinstance(val['value'], dict) and val['value']['type'] == 'pointer':
				# Duktape pointer type not yet supported.
				raise Exception('Pointer type not yet supported for builtins: %r' % val)
			else:
				# Property already in normalized form.
				repl_props.append(val)

			if obj['id'] == 'bi_date_prototype' and val['key'] == 'toUTCString':
				#print('Clone Date.prototype.toUTCString to Date.prototype.toGMTString')
				prop2 = copy.deepcopy(repl_props[-1])
				prop2['key'] = 'toGMTString'
				repl_props.append(prop2)

		# Replace properties with a variant where function properties
		# point to built-ins rather than using an inline syntax.
		obj['properties'] = repl_props

	len_before = len(meta['objects'])
	meta['objects'] += subobjs
	len_after = len(meta['objects'])

	print('Normalized metadata shorthand, %d objects -> %d final objects' % (len_before, len_after))

# Normalize property attribute order, default attributes, etc.
def metadata_normalize_property_attributes(meta):
	for o in meta['objects']:
		for p in o['properties']:
			orig_attrs = p.get('attributes', None)
			is_accessor = (isinstance(p['value'], dict) and p['value']['type'] == 'accessor')

			# If missing, set default attributes.
			attrs = orig_attrs
			if attrs is None:
				if is_accessor:
					attrs = 'ca'  # accessor default is configurable
				else:
					attrs = 'wc'  # default is writable, configurable
				#print('Defaulted attributes of %s/%s to %s' % (o['id'], p['key'], attrs))

			# Decode flags to normalize their order in the end.
			writable = 'w' in attrs
			enumerable = 'e' in attrs
			configurable = 'c' in attrs
			accessor = 'a' in attrs

			# Force 'accessor' attribute for accessors.
			if is_accessor and not accessor:
				#print('Property %s is accessor but has no "a" attribute, add attribute' % p['key'])
				accessor = True

			# Normalize order and write back.
			attrs = ''
			if writable:
				attrs += 'w'
			if enumerable:
				attrs += 'e'
			if configurable:
				attrs += 'c'
			if accessor:
				attrs += 'a'
			p['attributes'] = attrs

			if orig_attrs != attrs:
				#print('Updated attributes of %s/%s from %r to %r' % (o['id'], p['key'], orig_attrs, attrs))
				pass

# Normalize ROM property attributes.
def metadata_normalize_rom_property_attributes(meta):
	for o in meta['objects']:
		for p in o['properties']:
			# ROM properties must not be configurable (runtime code
			# depends on this).  Writability is kept so that instance
			# objects can override parent properties.
			p['attributes'] = p['attributes'].replace('c', '')

# Add a 'name' property for all top level functions; expected by RAM
# initialization code.
def metadata_normalize_ram_function_names(meta):
	for o in meta['objects']:
		if not o.get('callable', False):
			continue
		name_prop = None
		for p in o['properties']:
			if p['key'] == 'name':
				name_prop = p
				break
		if name_prop is None:
			print('Adding missing "name" property for top level function %s' % o['id'])
			o['properties'].append({ 'key': 'name', 'value': '', 'attributes': '' })

# Add a built-in objects list for RAM initialization.
def metadata_add_ram_filtered_object_list(meta):
	# For RAM init data to support user objects, we need to prepare a
	# filtered top level object list, containing only those objects which
	# need a value stack index during duk_hthread_builtins.c init process.
	#
	# Objects in meta['objects'] which are covered by inline property
	# notation in the init data (this includes e.g. member functions like
	# Math.cos) must not be present.

	objlist = []
	for o in meta['objects']:
		keep = o.get('bidx_used', False)
		if o.has_key('native') and not o.has_key('bidx'):
			# Handled inline by run-time init code
			pass
		else:
			# Top level object
			keep = True
		if keep:
			objlist.append(o)

	print('Filtered RAM object list: %d objects with bidx, %d total top level objects' % \
	      (len(meta['objects_bidx']), len(objlist)))

	meta['objects_ram_toplevel'] = objlist

# Add missing strings into strings metadata.  For example, if an object
# property key is not part of the strings list, append it there.  This
# is critical for ROM builtins because all property keys etc must also
# be in ROM.
def metadata_normalize_missing_strings(meta, user_meta):
	# We just need plain strings here.
	strs_have = {}
	for s in meta['strings']:
		strs_have[s['str']] = True

	# For ROM builtins all the strings must be in the strings list,
	# so scan objects for any strings not explicitly listed in metadata.
	for idx, obj in enumerate(meta['objects']):
		for prop in obj['properties']:
			key = prop['key']
			if not strs_have.get(key):
				#print('Add missing string: %r' % key)
				meta['strings'].append({ 'str': key, '_auto_add_ref': True })
				strs_have[key] = True
			if prop.has_key('value') and isinstance(prop['value'], (str, unicode)):
				val = unicode_to_bytes(prop['value'])  # XXX: should already be
				if not strs_have.get(val):
					#print('Add missing string: %r' % val)
					meta['strings'].append({ 'str': val, '_auto_add_ref': True })
					strs_have[val] = True

	# Force user strings to be in ROM data.
	for s in user_meta.get('add_forced_strings', []):
		if not strs_have.get(s['str']):
			#print('Add user string: %r' % s['str'])
			s['_auto_add_user'] = True
			meta['strings'].append(s)

# Detect objects not reachable from any object with a 'bidx'.  This is usually
# a user error because such objects can't be reached at runtime so they're
# useless in RAM or ROM init data.
def metadata_remove_orphan_objects(meta):
	reachable = {}

	for o in meta['objects']:
		if o.get('bidx_used', False):
			reachable[o['id']] = True

	while True:
		reachable_count = len(reachable.keys())

		def _markId(obj_id):
			if obj_id is None:
				return
			reachable[obj_id] = True

		for o in meta['objects']:
			if not reachable.has_key(o['id']):
				continue
			for p in o['properties']:
				# Shorthand has been normalized so no need
				# to support it here.
				v = p['value']
				ptype = None
				if isinstance(v, dict):
					ptype = p['value']['type']
				if ptype == 'object':
					_markId(v['id'])
				if ptype == 'accessor':
					_markId(v.get('getter_id'))
					_markId(v.get('setter_id'))

		print('Mark reachable: reachable count initially %d, now %d' % \
		      (reachable_count, len(reachable.keys())))
		if reachable_count == len(reachable.keys()):
			break

	deleted = True
	while deleted:
		deleted = False
		for i,o in enumerate(meta['objects']):
			if not reachable.has_key(o['id']):
				print('WARNING: object %s not reachable, dropping' % o['id'])
				meta['objects'].pop(i)
				deleted = True
				break

# Add C define names for builtin strings.  These defines are added to all
# strings, even when they won't get a stridx because the define names are
# used to autodetect referenced strings.
def metadata_add_string_define_names(strlist, special_defs):
	for s in strlist:
		v = s['str']

		if special_defs.has_key(v):
			s['define'] = 'DUK_STRIDX_' + special_defs[v]
			continue

		if len(v) >= 1 and v[0] == '\xff':
			pfx = 'DUK_STRIDX_INT_'
			v = v[1:]
		else:
			pfx = 'DUK_STRIDX_'

		t = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', v)  # add underscores: aB -> a_B
		s['define'] = pfx + t.upper()

# Add a 'stridx_used' flag for strings which need a stridx.
def metadata_add_string_used_stridx(strlist, used_stridx_meta):
	defs_needed = {}
	defs_found = {}
	for s in used_stridx_meta['used_stridx_defines']:
		defs_needed[s] = True

	# strings whose define is referenced
	for s in strlist:
		if s.has_key('define') and defs_needed.has_key(s['define']):
			s['stridx_used'] = True
			defs_found[s['define']] = True

	# duk_lexer.h needs all reserved words
	for s in strlist:
		if s.get('reserved_word', False):
			s['stridx_used'] = True

	# ensure all needed defines are provided
	defs_found['DUK_STRIDX_START_RESERVED'] = True  # special defines provided automatically
	defs_found['DUK_STRIDX_START_STRICT_RESERVED'] = True
	defs_found['DUK_STRIDX_END_RESERVED'] = True
	defs_found['DUK_STRIDX_TO_TOK'] = True
	for k in sorted(defs_needed.keys()):
		if not defs_found.has_key(k):
			raise Exception('source code needs define %s not provided by strings' % repr(k))

# Merge duplicate strings in string metadata.
def metadata_merge_string_entries(strlist):
	# The raw string list may contain duplicates so merge entries.
	# The list is processed in reverse because the last entry should
	# "win" and keep its place (this matters for reserved words).

	strs = []
	str_map = {}   # plain string -> object in strs[]
	tmp = copy.deepcopy(strlist)
	tmp.reverse()
	for s in tmp:
		prev = str_map.get(s['str'])
		if prev is not None:
			for k in s.keys():
				if prev.has_key(k) and prev[k] != s[k]:
					raise Exception('fail to merge string entry, conflicting keys: %r <-> %r' % (prev, s))
				prev[k] = s[k]
		else:
			strs.append(s)
			str_map[s['str']] = s
	strs.reverse()
	return strs

# Order builtin strings (strings with a stridx) into an order satisfying
# multiple constraints.
def metadata_order_builtin_strings(input_strlist, keyword_list, strip_unused_stridx=False):
	# Strings are ordered in the result as follows:
	#   1. Non-reserved words requiring 8-bit indices
	#   2. Non-reserved words not requiring 8-bit indices
	#   3. Reserved words in non-strict mode only
	#   4. Reserved words in strict mode
	#
	# Reserved words must follow an exact order because they are
	# translated to/from token numbers by addition/subtraction.
	# Some strings require an 8-bit index and must be in the
	# beginning.

	tmp_strs = []
	for s in copy.deepcopy(input_strlist):
		if not s.get('stridx_used', False):
			# Drop strings which are not actually needed by src/*.(c|h).
			# Such strings won't be in heap->strs[] or ROM legacy list.
			pass
		else:
			tmp_strs.append(s)

	# The reserved word list must match token order in duk_lexer.h
	# exactly, so pluck them out first.

	str_index = {}
	kw_index = {}
	keywords = []
	strs = []
	for idx,s in enumerate(tmp_strs):
		str_index[s['str']] = s
	for idx,s in enumerate(keyword_list):
		keywords.append(str_index[s])
		kw_index[s] = True
	for idx,s in enumerate(tmp_strs):
		if not kw_index.has_key(s['str']):
			strs.append(s)

	# Sort the strings by category number; within category keep
	# previous order.

	for idx,s in enumerate(strs):
		s['_idx'] = idx  # for ensuring stable sort

	def req8Bit(s):
		return s.get('class_name', False)   # currently just class names

	def getCat(s):
		req8 = req8Bit(s)
		if s.get('reserved_word', False):
			# XXX: unused path now, because keywords are "plucked out"
			# explicitly.
			assert(not req8)
			if s.get('future_reserved_word_strict', False):
				return 4
			else:
				return 3
		elif req8:
			return 1
		else:
			return 2

	def sortCmp(a,b):
		return cmp( (getCat(a),a['_idx']), (getCat(b),b['_idx']) )

	strs.sort(cmp=sortCmp)

	for idx,s in enumerate(strs):
		# Remove temporary _idx properties
		del s['_idx']

	for idx,s in enumerate(strs):
		if req8Bit(s) and i >= 256:
			raise Exception('8-bit string index not satisfied: ' + repr(s))

	return strs + keywords

# Dump metadata into a JSON file.
def dump_metadata(meta, fn):
	tmp = json.dumps(recursive_bytes_to_strings(meta), indent=4)
	with open(fn, 'wb') as f:
		f.write(tmp)
	print('Wrote metadata dump to %s' % fn)

# Main metadata loading function: load metadata from multiple sources,
# merge and normalize, prepare various indexes etc.
def load_metadata(opts, rom=False, build_info=None):
	# Load built-in strings and objects.
	with open(opts.strings_metadata, 'rb') as f:
		strings_metadata = recursive_strings_to_bytes(yaml.load(f))
	with open(opts.objects_metadata, 'rb') as f:
		objects_metadata = recursive_strings_to_bytes(yaml.load(f))

	# Merge strings and objects metadata as simple top level key merge.
	meta = {}
	for k in objects_metadata.keys():
		meta[k] = objects_metadata[k]
	for k in strings_metadata.keys():
		meta[k] = strings_metadata[k]

	# Add user objects.
	user_meta = {}
	for fn in opts.user_builtin_metadata:
		print('Merging user builtin metadata file %s' % fn)
		with open(fn, 'rb') as f:
			user_meta = recursive_strings_to_bytes(yaml.load(f))
		metadata_merge_user_objects(meta, user_meta)

	# Remove disabled objects and properties.
	metadata_remove_disabled(meta)

	# Normalize 'nargs' and 'length' defaults.
	metadata_normalize_nargs_length(meta)

	# Normalize property attributes.
	metadata_normalize_property_attributes(meta)

	# Normalize property shorthand into full objects.
	metadata_normalize_shorthand(meta)

	# RAM top-level functions must have a 'name'.
	if not rom:
		metadata_normalize_ram_function_names(meta)

	# Add Duktape.version and (Duktape.env for ROM case).
	for o in meta['objects']:
		if o['id'] == 'bi_duktape':
			o['properties'].insert(0, { 'key': 'version', 'value': int(build_info['version']), 'attributes': '' })
			if rom:
				# Use a fixed (quite dummy for now) Duktape.env
				# when ROM builtins are in use.  In the RAM case
				# this is added during global object initialization
				# based on config options in use.
				o['properties'].insert(0, { 'key': 'env', 'value': 'ROM', 'attributes': '' })

	# Normalize property attributes (just in case shorthand handling
	# didn't add attributes to all properties).
	metadata_normalize_property_attributes(meta)

	# For ROM objects, mark all properties non-configurable.
	if rom:
		metadata_normalize_rom_property_attributes(meta)

	# Create a list of objects needing a 'bidx'.  This is now just
	# based on the 'builtins' metadata list but could be dynamically
	# scanned somehow.  Ensure 'objects' and 'objects_bidx' match
	# in order for shared length.
	metadata_prepare_objects_bidx(meta)

	# Merge duplicate strings.
	meta['strings'] = metadata_merge_string_entries(meta['strings'])

	# Prepare an ordered list of strings with 'stridx':
	#   - Add a 'stridx_used' flag for strings which need an index in current code base
	#   - Add a C define (DUK_STRIDX_xxx) for such strings
	#   - Compute a stridx string order satisfying current runtime constraints
	#
	# The meta['strings_stridx'] result will be in proper order and stripped of
	# any strings which don't need a stridx.
	metadata_add_string_define_names(meta['strings'], meta['special_define_names'])
	with open(opts.used_stridx_metadata, 'rb') as f:
		metadata_add_string_used_stridx(meta['strings'], json.loads(f.read()))
	meta['strings_stridx'] = metadata_order_builtin_strings(meta['strings'], meta['reserved_word_token_order'])

	# For the ROM build: add any strings referenced by built-in objects
	# into the string list (not the 'stridx' list though): all strings
	# referenced by ROM objects must also be in ROM.
	if rom:
		for fn in opts.user_builtin_metadata:
			# XXX: awkward second pass
			with open(fn, 'rb') as f:
				user_meta = recursive_strings_to_bytes(yaml.load(f))
				metadata_normalize_missing_strings(meta, user_meta)
		metadata_normalize_missing_strings(meta, {})  # in case no files

	# Check for orphan objects and remove them.
	metadata_remove_orphan_objects(meta)

	# Add final stridx and bidx indices to metadata objects and strings.
	idx = 0
	for o in meta['objects']:
		if o.get('bidx_used', False):
			o['bidx'] = idx
			idx += 1
	idx = 0
	for s in meta['strings']:
		if s.get('stridx_used', False):
			s['stridx'] = idx
			idx += 1

	# Prepare a filtered RAM top level object list, needed for technical
	# reasons during RAM init handling.
	if not rom:
		metadata_add_ram_filtered_object_list(meta)

	# Sanity check: object index must match 'bidx' for all objects
	# which have a runtime 'bidx'.  This is assumed by e.g. RAM
	# thread init.
	for i,o in enumerate(meta['objects']):
		if i < len(meta['objects_bidx']):
			assert(meta['objects_bidx'][i] == meta['objects'][i])
		if o.has_key('bidx'):
			assert(o['bidx'] == i)

	# Create a set of helper lists and maps now that the metadata is
	# in its final form.
	meta['_strings_plain'] = []
	meta['_strings_stridx_plain'] = []
	meta['_stridx_to_string'] = {}
	meta['_idx_to_string'] = {}
	meta['_stridx_to_plain'] = {}
	meta['_idx_to_plain'] = {}
	meta['_string_to_stridx'] = {}
	meta['_plain_to_stridx'] = {}
	meta['_string_to_idx'] = {}
	meta['_plain_to_idx'] = {}
	meta['_define_to_stridx'] = {}
	meta['_stridx_to_define'] = {}
	meta['_is_plain_reserved_word'] = {}
	meta['_is_plain_strict_reserved_word'] = {}
	meta['_objid_to_object'] = {}
	meta['_objid_to_bidx'] = {}
	meta['_objid_to_idx'] = {}
	meta['_objid_to_ramidx'] = {}
	meta['_bidx_to_objid'] = {}
	meta['_idx_to_objid'] = {}
	meta['_bidx_to_object'] = {}
	meta['_idx_to_object'] = {}

	for i,s in enumerate(meta['strings']):
		assert(s['str'] not in meta['_strings_plain'])
		meta['_strings_plain'].append(s['str'])
		if s.get('reserved_word', False):
			meta['_is_plain_reserved_word'][s['str']] = True  # includes also strict reserved words
		if s.get('future_reserved_word_strict', False):
			meta['_is_plain_strict_reserved_word'][s['str']] = True
		meta['_idx_to_string'][i] = s
		meta['_idx_to_plain'][i] = s['str']
		meta['_plain_to_idx'][s['str']] = i
		#meta['_string_to_idx'][s] = i
	for i,s in enumerate(meta['strings_stridx']):
		assert(s.get('stridx_used', False) == True)
		meta['_strings_stridx_plain'].append(s['str'])
		meta['_stridx_to_string'][i] = s
		meta['_stridx_to_plain'][i] = s['str']
		#meta['_string_to_stridx'][s] = i
		meta['_plain_to_stridx'][s['str']] = i
		meta['_define_to_stridx'][s['define']] = i
		meta['_stridx_to_define'][i] = s['define']
	for i,o in enumerate(meta['objects']):
		meta['_objid_to_object'][o['id']] = o
		meta['_objid_to_idx'][o['id']] = i
		meta['_idx_to_objid'][i] = o['id']
		meta['_idx_to_object'][i] = o
	for i,o in enumerate(meta['objects_bidx']):
		assert(o.get('bidx_used', False) == True)
		meta['_objid_to_bidx'][o['id']] = i
		meta['_bidx_to_objid'][i] = o['id']
		meta['_bidx_to_object'][i] = o
	if meta.has_key('objects_ram_toplevel'):
		for i,o in enumerate(meta['objects_ram_toplevel']):
			meta['_objid_to_ramidx'][o['id']] = i

	# Dump stats.

	if rom:
		meta_name = 'ROM'
	else:
		meta_name = 'RAM'

	count_add_ref = 0
	count_add_user = 0
	for s in meta['strings']:
		if s.get('_auto_add_ref', False):
			count_add_ref += 1
		if s.get('_auto_add_user', False):
			count_add_user += 1
	count_add = count_add_ref + count_add_user

	print(('Prepared %s metadata: %d objects, %d objects with bidx, ' + \
	       '%d strings, %d strings with stridx, %d strings added ' + \
	       '(%d property key references, %d user strings)') % \
	      (meta_name, len(meta['objects']), len(meta['objects_bidx']), \
	       len(meta['strings']), len(meta['strings_stridx']), \
	       count_add, count_add_ref, count_add_user))

	return meta

#
#  Metadata helpers
#

# Magic values for Math built-in.
math_onearg_magic = {
	'fabs': 0,   # BI_MATH_FABS_IDX
	'acos': 1,   # BI_MATH_ACOS_IDX
	'asin': 2,   # BI_MATH_ASIN_IDX
	'atan': 3,   # BI_MATH_ATAN_IDX
	'ceil': 4,   # BI_MATH_CEIL_IDX
	'cos': 5,    # BI_MATH_COS_IDX
	'exp': 6,    # BI_MATH_EXP_IDX
	'floor': 7,  # BI_MATH_FLOOR_IDX
	'log': 8,    # BI_MATH_LOG_IDX
	'round': 9,  # BI_MATH_ROUND_IDX
	'sin': 10,   # BI_MATH_SIN_IDX
	'sqrt': 11,  # BI_MATH_SQRT_IDX
	'tan': 12    # BI_MATH_TAN_IDX
}
math_twoarg_magic = {
	'atan2': 0,  # BI_MATH_ATAN2_IDX
	'pow': 1     # BI_MATH_POW_IDX
}

# Magic values for Array built-in.
array_iter_magic = {
	'every': 0,    # BI_ARRAY_ITER_EVERY
	'some': 1,     # BI_ARRAY_ITER_SOME
	'forEach': 2,  # BI_ARRAY_ITER_FOREACH
	'map': 3,      # BI_ARRAY_ITER_MAP
	'filter': 4    # BI_ARRAY_ITER_FILTER
}

# Magic value for typedarray/node.js buffer read field operations.
def magic_readfield(elem, signed=None, bigendian=None, typedarray=None):
	# Must match duk__FLD_xxx in duk_bi_buffer.c
	elemnum = {
		'8bit': 0,
		'16bit': 1,
		'32bit': 2,
		'float': 3,
		'double': 4,
		'varint': 5
	}[elem]
	if signed == True:
		signednum = 1
	elif signed == False:
		signednum = 0
	else:
		raise Exception('missing "signed"')
	if bigendian == True:
		bigendiannum = 1
	elif bigendian == False:
		bigendiannum = 0
	else:
		raise Exception('missing "bigendian"')
	if typedarray == True:
		typedarraynum = 1
	elif typedarray == False:
		typedarraynum = 0
	else:
		raise Exception('missing "typedarray"')
	return elemnum + (signednum << 4) + (bigendiannum << 3) + (typedarraynum << 5)

# Magic value for typedarray/node.js buffer write field operations.
def magic_writefield(elem, signed=None, bigendian=None, typedarray=None):
	return magic_readfield(elem, signed=signed, bigendian=bigendian, typedarray=typedarray)

# Magic value for typedarray constructors.
def magic_typedarray_constructor(elem, shift):
	# Must match duk_hbufobj.h header
	elemnum = {
		'uint8': 0,
		'uint8clamped': 1,
		'int8': 2,
		'uint16': 3,
		'int16': 4,
		'uint32': 5,
		'int32': 6,
		'float32': 7,
		'float64': 8
	}[elem]
	return (elemnum << 2) + shift

# Resolve a magic value from a YAML metadata element into an integer.
def resolve_magic(elem, objid_to_bidx):
	if elem is None:
		return 0
	if isinstance(elem, (int, long)):
		v = int(elem)
		if not (v >= -0x8000 and v <= 0x7fff):
			raise Exception('invalid plain value for magic: %s' % repr(v))
		# Magic is a 16-bit signed value, but convert to 16-bit signed
		# for encoding
		return v & 0xffff
	if not isinstance(elem, dict):
		raise Exception('invalid magic: %r' % elem)

	assert(elem.has_key('type'))
	if elem['type'] == 'bidx':
		# Maps to thr->builtins[].
		v = elem['id']
		return objid_to_bidx[v]
	elif elem['type'] == 'plain':
		v = elem['value']
		if not (v >= -0x8000 and v <= 0x7fff):
			raise Exception('invalid plain value for magic: %s' % repr(v))
		# Magic is a 16-bit signed value, but convert to 16-bit signed
		# for encoding
		return v & 0xffff
	elif elem['type'] == 'math_onearg':
		return math_onearg_magic[elem['funcname']]
	elif elem['type'] == 'math_twoarg':
		return math_twoarg_magic[elem['funcname']]
	elif elem['type'] == 'array_iter':
		return array_iter_magic[elem['funcname']]
	elif elem['type'] == 'typedarray_constructor':
		return magic_typedarray_constructor(elem['elem'], elem['shift'])
	elif elem['type'] == 'buffer_readfield':
		return magic_readfield(elem['elem'], elem['signed'], elem['bigendian'], elem['typedarray'])
	elif elem['type'] == 'buffer_writefield':
		return magic_writefield(elem['elem'], elem['signed'], elem['bigendian'], elem['typedarray'])
	else:
		raise Exception('invalid magic type: %r' % elem)

# Helper to find a property from a property list, remove it from the
# property list, and return the removed property.
def steal_prop(props, key):
	for idx,prop in enumerate(props):
		if prop['key'] == key:
			return props.pop(idx)
	return None

#
#  RAM initialization data
#
#  Init data for built-in strings and objects.  The init data for both
#  strings and objects is a bit-packed stream tailored to match the decoders
#  in duk_heap_alloc.c (strings) and duk_hthread_builtins.c (objects).
#  Various bitfield sizes are used to minimize the bitstream size without
#  resorting to actual, expensive compression.  The goal is to minimize the
#  overall size of the init code and the init data.
#
#  The built-in data created here is used to set up initial RAM versions
#  of the strings and objects.  References to these objects are tracked in
#  heap->strs[] and thr->builtins[] which allows Duktape internals to refer
#  to built-ins e.g. as thr->builtins[DUK_BIDX_STRING_PROTOTYPE].
#
#  Not all strings and objects need to be reachable through heap->strs[]
#  or thr->builtins[]: the strings/objects that need to be in these arrays
#  is determined based on metadata and source code scanning.
#

# XXX: Reserved word stridxs could be made to match token numbers
#      directly so that a duk_stridx2token[] would not be needed.

# Default property attributes, see E5 Section 15 beginning.
LENGTH_PROPERTY_ATTRIBUTES = ''
ACCESSOR_PROPERTY_ATTRIBUTES = 'c'
DEFAULT_DATA_PROPERTY_ATTRIBUTES = 'wc'

# Encoding constants (must match duk_hthread_builtins.c).
CLASS_BITS = 5
BIDX_BITS = 7
STRIDX_BITS = 9   # would be nice to optimize to 8
NATIDX_BITS = 8
NUM_NORMAL_PROPS_BITS = 6
NUM_FUNC_PROPS_BITS = 6
PROP_FLAGS_BITS = 3
STRING_LENGTH_BITS = 8
STRING_CHAR_BITS = 7
LENGTH_PROP_BITS = 3
NARGS_BITS = 3
PROP_TYPE_BITS = 3
MAGIC_BITS = 16

NARGS_VARARGS_MARKER = 0x07
NO_CLASS_MARKER = 0x00   # 0 = DUK_HOBJECT_CLASS_UNUSED
NO_BIDX_MARKER = 0x7f
NO_STRIDX_MARKER = 0xff

PROP_TYPE_DOUBLE = 0
PROP_TYPE_STRING = 1
PROP_TYPE_STRIDX = 2
PROP_TYPE_BUILTIN = 3
PROP_TYPE_UNDEFINED = 4
PROP_TYPE_BOOLEAN_TRUE = 5
PROP_TYPE_BOOLEAN_FALSE = 6
PROP_TYPE_ACCESSOR = 7

# must match duk_hobject.h
PROPDESC_FLAG_WRITABLE =     (1 << 0)
PROPDESC_FLAG_ENUMERABLE =   (1 << 1)
PROPDESC_FLAG_CONFIGURABLE = (1 << 2)
PROPDESC_FLAG_ACCESSOR =     (1 << 3)  # unused now

# Class names, numeric indices must match duk_hobject.h class numbers.
class_names = [
	'Unused',
	'Arguments',
	'Array',
	'Boolean',
	'Date',
	'Error',
	'Function',
	'JSON',
	'Math',
	'Number',
	'Object',
	'RegExp',
	'String',
	'global',
	'ObjEnv',
	'DecEnv',
	'Buffer',
	'Pointer',
	'Thread',
]
class2num = {}
for i,v in enumerate(class_names):
	class2num[v] = i

# Map class name to a class number.
def class_to_number(x):
	return class2num[x]

# Generate bit-packed RAM string init data.
def gen_ramstr_initdata_bitpacked(meta):
	be = dukutil.BitEncoder()

	# Strings are encoded as follows: a string begins in lowercase
	# mode and recognizes the following 5-bit symbols:
	#
	#    0-25    'a' ... 'z'
	#    26	     '_'
	#    27      0x00 (actually decoded to 0xff, internal marker)
	#    28	     reserved
	#    29      switch to uppercase for one character
	#            (next 5-bit symbol must be in range 0-25)
	#    30      switch to uppercase
	#    31      read a 7-bit character verbatim
	#
	# Uppercase mode is the same except codes 29 and 30 switch to
	# lowercase.

	UNDERSCORE = 26
	ZERO = 27
	SWITCH1 = 29
	SWITCH = 30
	SEVENBIT = 31

	maxlen = 0
	n_optimal = 0
	n_switch1 = 0
	n_switch = 0
	n_sevenbit = 0

	for s_obj in meta['strings_stridx']:
		s = s_obj['str']

		be.bits(len(s), 5)

		if len(s) > maxlen:
			maxlen = len(s)

		# 5-bit character, mode specific
		mode = 'lowercase'

		for idx, c in enumerate(s):
			# This encoder is not that optimal, but good enough for now.

			islower = (ord(c) >= ord('a') and ord(c) <= ord('z'))
			isupper = (ord(c) >= ord('A') and ord(c) <= ord('Z'))
			islast = (idx == len(s) - 1)
			isnextlower = False
			isnextupper = False
			if not islast:
				c2 = s[idx+1]
				isnextlower = (ord(c2) >= ord('a') and ord(c2) <= ord('z'))
				isnextupper = (ord(c2) >= ord('A') and ord(c2) <= ord('Z'))

			if c == '_':
				be.bits(UNDERSCORE, 5)
				n_optimal += 1
			elif c == '\xff':
				# A 0xff prefix (never part of valid UTF-8) is used for internal properties.
				# It is encoded as 0x00 in generated init data for technical reasons: it
				# keeps lookup table elements 7 bits instead of 8 bits.
				be.bits(ZERO, 5)
				n_optimal += 1
			elif islower and mode == 'lowercase':
				be.bits(ord(c) - ord('a'), 5)
				n_optimal += 1
			elif isupper and mode == 'uppercase':
				be.bits(ord(c) - ord('A'), 5)
				n_optimal += 1
			elif islower and mode == 'uppercase':
				if isnextlower:
					be.bits(SWITCH, 5)
					be.bits(ord(c) - ord('a'), 5)
					mode = 'lowercase'
					n_switch += 1
				else:
					be.bits(SWITCH1, 5)
					be.bits(ord(c) - ord('a'), 5)
					n_switch1 += 1
			elif isupper and mode == 'lowercase':
				if isnextupper:
					be.bits(SWITCH, 5)
					be.bits(ord(c) - ord('A'), 5)
					mode = 'uppercase'
					n_switch += 1
				else:
					be.bits(SWITCH1, 5)
					be.bits(ord(c) - ord('A'), 5)
					n_switch1 += 1
			else:
				assert(ord(c) >= 0 and ord(c) <= 127)
				be.bits(SEVENBIT, 5)
				be.bits(ord(c), 7)
				n_sevenbit += 1
				#print('sevenbit for: %r' % c)

	# end marker not necessary, C code knows length from define

	res = be.getByteString()

	print('%d ram strings, %d bytes of string init data, %d maximum string length, ' + \
	      'encoding: optimal=%d,switch1=%d,switch=%d,sevenbit=%d') % \
	      (len(meta['strings_stridx']), len(res), maxlen, \
	      n_optimal, n_switch1, n_switch, n_sevenbit)

	return res, maxlen

# Functions to emit string-related source/header parts.

def emit_ramstr_source_strinit_data(genc, strdata):
	genc.emitArray(strdata, 'duk_strings_data', visibility='DUK_INTERNAL', typename='duk_uint8_t', intvalues=True, const=True, size=len(strdata))

def emit_ramstr_header_strinit_defines(genc, meta, strdata, strmaxlen):
	genc.emitLine('#if !defined(DUK_SINGLE_FILE)')
	genc.emitLine('DUK_INTERNAL_DECL const duk_uint8_t duk_strings_data[%d];' % len(strdata))
	genc.emitLine('#endif  /* !DUK_SINGLE_FILE */')
	genc.emitDefine('DUK_STRDATA_MAX_STRLEN', strmaxlen)
	genc.emitDefine('DUK_STRDATA_DATA_LENGTH', len(strdata))

# This is used for both RAM and ROM strings.
def emit_header_stridx_defines(genc, meta):
	strlist = meta['strings_stridx']

	for idx,s in enumerate(strlist):
		genc.emitDefine(s['define'], idx, repr(s['str']))
		defname = s['define'].replace('_STRIDX','_HEAP_STRING')
		genc.emitDefine(defname + '(heap)', 'DUK_HEAP_GET_STRING((heap),%s)' % s['define'])
		defname = s['define'].replace('_STRIDX', '_HTHREAD_STRING')
		genc.emitDefine(defname + '(thr)', 'DUK_HTHREAD_GET_STRING((thr),%s)' % s['define'])

	idx_start_reserved = None
	idx_start_strict_reserved = None
	for idx,s in enumerate(strlist):
		if idx_start_reserved is None and s.get('reserved_word', False):
			idx_start_reserved = idx
		if idx_start_strict_reserved is None and s.get('future_reserved_word_strict', False):
			idx_start_strict_reserved = idx
	assert(idx_start_reserved is not None)
	assert(idx_start_strict_reserved is not None)

	genc.emitLine('')
	genc.emitDefine('DUK_HEAP_NUM_STRINGS', len(strlist))
	genc.emitDefine('DUK_STRIDX_START_RESERVED', idx_start_reserved)
	genc.emitDefine('DUK_STRIDX_START_STRICT_RESERVED', idx_start_strict_reserved)
	genc.emitDefine('DUK_STRIDX_END_RESERVED', len(strlist), comment='exclusive endpoint')
	genc.emitLine('')
	genc.emitLine('/* To convert a heap stridx to a token number, subtract')
	genc.emitLine(' * DUK_STRIDX_START_RESERVED and add DUK_TOK_START_RESERVED.')
	genc.emitLine(' */')

# Encode property flags for RAM initializers.
def encode_property_flags(flags):
	# Note: must match duk_hobject.h

	res = 0
	nflags = 0
	if 'w' in flags:
		nflags += 1
		res = res | PROPDESC_FLAG_WRITABLE
	if 'e' in flags:
		nflags += 1
		res = res | PROPDESC_FLAG_ENUMERABLE
	if 'c' in flags:
		nflags += 1
		res = res | PROPDESC_FLAG_CONFIGURABLE
	if 'a' in flags:
		nflags += 1
		res = res | PROPDESC_FLAG_ACCESSOR

	if nflags != len(flags):
		raise Exception('unsupported flags: %s' % repr(flags))

	return res

# Generate RAM object initdata for an object (but not its properties).
def gen_ramobj_initdata_for_object(meta, be, bi, string_to_stridx, natfunc_name_to_natidx, objid_to_bidx):
	def _stridx(strval):
		stridx = string_to_stridx[strval]
		be.bits(stridx, STRIDX_BITS)
	def _stridx_or_string(strval):
		# XXX: could share the built-in strings decoder, would save ~200 bytes.
		stridx = string_to_stridx.get(strval)
		if stridx is not None:
			be.bits(0, 1)  # marker: stridx
			be.bits(stridx, STRIDX_BITS)
		else:
			be.bits(1, 1)  # marker: raw bytes
			be.bits(len(strval), STRING_LENGTH_BITS)
			for i in xrange(len(strval)):
				be.bits(ord(strval[i]), STRING_CHAR_BITS)
	def _natidx(native_name):
		natidx = natfunc_name_to_natidx[native_name]
		be.bits(natidx, NATIDX_BITS)

	class_num = class_to_number(bi['class'])
	be.bits(class_num, CLASS_BITS)

	props = [x for x in bi['properties']]  # clone

	prop_proto = steal_prop(props, 'prototype')
	prop_constr = steal_prop(props, 'constructor')
	prop_name = steal_prop(props, 'name')
	prop_length = steal_prop(props, 'length')

	length = -1  # default value -1 signifies varargs
	if prop_length is not None:
		assert(isinstance(prop_length['value'], int))
		length = prop_length['value']
		be.bits(1, 1)  # flag: have length
		be.bits(length, LENGTH_PROP_BITS)
	else:
		be.bits(0, 1)  # flag: no length

	# The attributes for 'length' are standard ("none") except for
	# Array.prototype.length which must be writable (this is handled
	# separately in duk_hthread_builtins.c).

	len_attrs = LENGTH_PROPERTY_ATTRIBUTES
	if prop_length is not None:
		len_attrs = prop_length['attributes']

	if len_attrs != LENGTH_PROPERTY_ATTRIBUTES:
		# Attributes are assumed to be the same, except for Array.prototype.
		if bi['class'] != 'Array':  # Array.prototype is the only one with this class
			raise Exception('non-default length attribute for unexpected object')

	# For 'Function' classed objects, emit the native function stuff.
	# Unfortunately this is more or less a copy of what we do for
	# function properties now.  This should be addressed if a rework
	# on the init format is done.

	if bi['class'] == 'Function':
		_natidx(bi['native'])

		if bi.get('varargs', False):
			be.bits(1, 1)  # flag: non-default nargs
			be.bits(NARGS_VARARGS_MARKER, NARGS_BITS)
		elif bi.has_key('nargs') and bi['nargs'] != length:
			be.bits(1, 1)  # flag: non-default nargs
			be.bits(bi['nargs'], NARGS_BITS)
		else:
			assert(length is not None)
			be.bits(0, 1)  # flag: default nargs OK

		# All Function-classed global level objects are callable
		# (have [[Call]]) but not all are constructable (have
		# [[Construct]]).  Flag that.

		assert(bi.has_key('callable'))
		assert(bi['callable'] == True)

		assert(prop_name is not None)
		assert(isinstance(prop_name['value'], str))
		_stridx_or_string(prop_name['value'])

		if bi.get('constructable', False):
			be.bits(1, 1)	# flag: constructable
		else:
			be.bits(0, 1)	# flag: not constructable

		magic = resolve_magic(bi.get('magic'), objid_to_bidx)
		if magic != 0:
			assert(magic >= 0)
			assert(magic < (1 << MAGIC_BITS))
			be.bits(1, 1)
			be.bits(magic, MAGIC_BITS)
		else:
			be.bits(0, 1)

# Generate RAM object initdata for an object's properties.
def gen_ramobj_initdata_for_props(meta, be, bi, string_to_stridx, natfunc_name_to_natidx, objid_to_bidx, double_byte_order):
	count_normal_props = 0
	count_function_props = 0

	def _bidx(bi_id):
		if bi_id is None:
			be.bits(NO_BIDX_MARKER, BIDX_BITS)
		else:
			be.bits(objid_to_bidx[bi_id], BIDX_BITS)
	def _stridx(strval):
		stridx = string_to_stridx[strval]
		be.bits(stridx, STRIDX_BITS)
	def _stridx_or_string(strval):
		# XXX: could share the built-in strings decoder, would save ~200 bytes.
		stridx = string_to_stridx.get(strval)
		if stridx is not None:
			be.bits(0, 1)  # marker: stridx
			be.bits(stridx, STRIDX_BITS)
		else:
			be.bits(1, 1)  # marker: raw bytes
			be.bits(len(strval), STRING_LENGTH_BITS)
			for i in xrange(len(strval)):
				be.bits(ord(strval[i]), STRING_CHAR_BITS)
	def _natidx(native_name):
		natidx = natfunc_name_to_natidx[native_name]
		be.bits(natidx, NATIDX_BITS)

	props = [x for x in bi['properties']]  # clone

	# internal prototype: not an actual property so not in property list
	if bi.has_key('internal_prototype'):
		_bidx(bi['internal_prototype'])
	else:
		_bidx(None)

	# external prototype: encoded specially, steal from property list
	prop_proto = steal_prop(props, 'prototype')
	if prop_proto is not None:
		assert(prop_proto['value']['type'] == 'object')
		assert(prop_proto['attributes'] == '')
		_bidx(prop_proto['value']['id'])
	else:
		_bidx(None)

	# external constructor: encoded specially, steal from property list
	prop_constr = steal_prop(props, 'constructor')
	if prop_constr is not None:
		assert(prop_constr['value']['type'] == 'object')
		assert(prop_constr['attributes'] == 'wc')
		_bidx(prop_constr['value']['id'])
	else:
		_bidx(None)

	# name: encoded specially for function objects, so steal and ignore here
	if bi['class'] == 'Function':
		prop_name = steal_prop(props, 'name')
		assert(prop_name is not None)
		assert(isinstance(prop_name['value'], str))
		# Function.prototype.name has special handling in duk_hthread_builtins.c
		assert((bi['id'] != 'bi_function_prototype' and prop_name['attributes'] == '') or \
		       (bi['id'] == 'bi_function_prototype' and prop_name['attributes'] == 'w'))

	# length: encoded specially, so steal and ignore
	prop_proto = steal_prop(props, 'length')

	# Date.prototype.toGMTString needs special handling and is handled
	# directly in duk_hthread_builtins.c; so steal and ignore here.
	if bi['id'] == 'bi_date_prototype':
		prop_togmtstring = steal_prop(props, 'toGMTString')
		assert(prop_togmtstring is not None)
		#print('Stole Date.prototype.toGMTString')

	# Split properties into non-builtin functions and other properties.
	# This split is a bit arbitrary, but is used to reduce flag bits in
	# the bit stream.
	values = []
	functions = []
	for prop in props:
		if isinstance(prop['value'], dict) and \
		   prop['value']['type'] == 'object' and \
		   metadata_lookup_object(meta, prop['value']['id']).has_key('native') and \
		   not metadata_lookup_object(meta, prop['value']['id']).has_key('bidx'):
			functions.append(prop)
		else:
			values.append(prop)

	be.bits(len(values), NUM_NORMAL_PROPS_BITS)

	for valspec in values:
		count_normal_props += 1

		val = valspec['value']

		_stridx_or_string(valspec['key'])

		# Attribute check doesn't check for accessor flag; that is now
		# automatically set by C code when value is an accessor type.
		# Accessors must not have 'writable', so they'll always have
		# non-default attributes (less footprint than adding a different
		# default).
		default_attrs = DEFAULT_DATA_PROPERTY_ATTRIBUTES

		attrs = valspec.get('attributes', default_attrs)
		attrs = attrs.replace('a', '')  # ram bitstream doesn't encode 'accessor' attribute
		if attrs != default_attrs:
			#print('non-default attributes: %s -> %r (default %r)' % (valspec['key'], attrs, default_attrs))
			be.bits(1, 1)  # flag: have custom attributes
			be.bits(encode_property_flags(attrs), PROP_FLAGS_BITS)
		else:
			be.bits(0, 1)  # flag: no custom attributes

		if val is None:
			print('WARNING: RAM init data format doesn\'t support "null" now, value replaced with "undefined": %r' % valspec)
			#raise Exception('RAM init format doesn\'t support a "null" value now')
			be.bits(PROP_TYPE_UNDEFINED, PROP_TYPE_BITS)
		elif isinstance(val, bool):
			if val == True:
				be.bits(PROP_TYPE_BOOLEAN_TRUE, PROP_TYPE_BITS)
			else:
				be.bits(PROP_TYPE_BOOLEAN_FALSE, PROP_TYPE_BITS)
		elif isinstance(val, (float, int)) or isinstance(val, dict) and val['type'] == 'double':
			# Avoid converting a manually specified NaN temporarily into
			# a float to avoid risk of e.g. NaN being replaced by another.
			if isinstance(val, dict):
				val = val['bytes'].decode('hex')
				assert(len(val) == 8)
			else:
				val = struct.pack('>d', float(val))

			be.bits(PROP_TYPE_DOUBLE, PROP_TYPE_BITS)

			# encoding of double must match target architecture byte order
			indexlist = {
				'big':    [ 0, 1, 2, 3, 4, 5, 6, 7 ],
				'little': [ 7, 6, 5, 4, 3, 2, 1, 0 ],
				'mixed':  [ 3, 2, 1, 0, 7, 6, 5, 4 ]    # some arm platforms
			}[double_byte_order]

			data = ''.join([ val[indexlist[idx]] for idx in xrange(8) ])

			#print('DOUBLE: %s -> %s' % (val.encode('hex'), data.encode('hex')))

			if len(data) != 8:
				raise Exception('internal error')
			be.string(data)
		elif isinstance(val, str) or isinstance(val, unicode):
			if isinstance(val, unicode):
				# Note: non-ASCII characters will not currently work,
				# because bits/char is too low.
				val = val.encode('utf-8')

			if string_to_stridx.has_key(val):
				# String value is in built-in string table -> encode
				# using a string index.  This saves some space,
				# especially for the 'name' property of errors
				# ('EvalError' etc).

				be.bits(PROP_TYPE_STRIDX, PROP_TYPE_BITS)
				_stridx(val)
			else:
				# Not in string table -> encode as raw 7-bit value

				be.bits(PROP_TYPE_STRING, PROP_TYPE_BITS)
				be.bits(len(val), STRING_LENGTH_BITS)
				for i in xrange(len(val)):
					be.bits(ord(val[i]), STRING_CHAR_BITS)
		elif isinstance(val, dict):
			if val['type'] == 'object':
				be.bits(PROP_TYPE_BUILTIN, PROP_TYPE_BITS)
				_bidx(val['id'])
			elif val['type'] == 'undefined':
				be.bits(PROP_TYPE_UNDEFINED, PROP_TYPE_BITS)
			elif val['type'] == 'accessor':
				be.bits(PROP_TYPE_ACCESSOR, PROP_TYPE_BITS)
				getter_fn = metadata_lookup_object(meta, val['getter_id'])
				setter_fn = metadata_lookup_object(meta, val['setter_id'])
				_natidx(getter_fn['native'])
				_natidx(setter_fn['native'])
				assert(getter_fn['nargs'] == 0)
				assert(setter_fn['nargs'] == 1)
				assert(getter_fn['magic'] == 0)
				assert(setter_fn['magic'] == 0)
			else:
				raise Exception('unsupported value: %s' % repr(val))
		else:
			raise Exception('unsupported value: %s' % repr(val))

	be.bits(len(functions), NUM_FUNC_PROPS_BITS)

	for funprop in functions:
		count_function_props += 1

		funobj = metadata_lookup_object(meta, funprop['value']['id'])
		prop_len = metadata_lookup_property(funobj, 'length')
		assert(prop_len is not None)
		assert(isinstance(prop_len['value'], (int)))
		length = prop_len['value']

		_stridx_or_string(funprop['key'])
		_natidx(funobj['native'])
		be.bits(length, LENGTH_PROP_BITS)

		if funobj.get('varargs', False):
			be.bits(1, 1)  # flag: non-default nargs
			be.bits(NARGS_VARARGS_MARKER, NARGS_BITS)
		elif funobj.has_key('nargs') and funobj['nargs'] != length:
			be.bits(1, 1)  # flag: non-default nargs
			be.bits(funobj['nargs'], NARGS_BITS)
		else:
			be.bits(0, 1)  # flag: default nargs OK

		# XXX: make this check conditional to minimize bit count
		# (there are quite a lot of function properties)
		magic = resolve_magic(funobj.get('magic'), objid_to_bidx)
		if magic != 0:
			assert(magic >= 0)
			assert(magic < (1 << MAGIC_BITS))
			be.bits(1, 1)
			be.bits(magic, MAGIC_BITS)
		else:
			be.bits(0, 1)

	return count_normal_props, count_function_props

# Get helper maps for RAM objects.
def get_ramobj_native_func_maps(meta):
	# Native function list and index
	native_funcs_found = {}
	native_funcs = []
	natfunc_name_to_natidx = {}

	for o in meta['objects']:
		if o.has_key('native'):
			native_funcs_found[o['native']] = True
		for v in o['properties']:
			val = v['value']
			if isinstance(val, dict):
				if val['type'] == 'accessor':
					getter = metadata_lookup_object(meta, val['getter_id'])
					native_funcs_found[getter['native']] = True
					setter = metadata_lookup_object(meta, val['setter_id'])
					native_funcs_found[setter['native']] = True
				if val['type'] == 'object':
					target = metadata_lookup_object(meta, val['id'])
					if target.has_key('native'):
						native_funcs_found[target['native']] = True
	for idx,k in enumerate(sorted(native_funcs_found.keys())):
		native_funcs.append(k)  # native func names
		natfunc_name_to_natidx[k] = idx

	return native_funcs, natfunc_name_to_natidx

# Generate bit-packed RAM object init data.
def gen_ramobj_initdata_bitpacked(meta, native_funcs, natfunc_name_to_natidx, double_byte_order):
	# RAM initialization is based on a specially filtered list of top
	# level objects which includes objects with 'bidx' and objects
	# which aren't handled as inline values in the init bitstream.
	objlist = meta['objects_ram_toplevel']
	objid_to_idx = meta['_objid_to_ramidx']
	objid_to_object = meta['_objid_to_object']  # This index is valid even for filtered object list
	string_index = meta['_plain_to_stridx']

	# Generate bitstream
	be = dukutil.BitEncoder()
	count_builtins = 0
	count_normal_props = 0
	count_function_props = 0
	for o in objlist:
		count_builtins += 1
		gen_ramobj_initdata_for_object(meta, be, o, string_index, natfunc_name_to_natidx, objid_to_idx)
	for o in objlist:
		count_obj_normal, count_obj_func = gen_ramobj_initdata_for_props(meta, be, o, string_index, natfunc_name_to_natidx, objid_to_idx, double_byte_order)
		count_normal_props += count_obj_normal
		count_function_props += count_obj_func

	romobj_init_data = be.getByteString()
	#print(repr(romobj_init_data))
	#print(len(romobj_init_data))

	print('%d ram builtins, %d normal properties, %d function properties, %d bytes of object init data' % \
	      (count_builtins, count_normal_props, count_function_props, len(romobj_init_data)))

	return romobj_init_data

# Functions to emit object-related source/header parts.

def emit_ramobj_source_nativefunc_array(genc, native_func_list):
	genc.emitLine('/* native functions: %d */' % len(native_func_list))
	genc.emitLine('DUK_INTERNAL const duk_c_function duk_bi_native_functions[%d] = {' % len(native_func_list))
	for i in native_func_list:
		# The function pointer cast here makes BCC complain about
		# "initializer too complicated", so omit the cast.
		#genc.emitLine('\t(duk_c_function) %s,' % i)
		genc.emitLine('\t%s,' % i)
	genc.emitLine('};')

def emit_ramobj_source_objinit_data(genc, init_data):
	genc.emitArray(init_data, 'duk_builtins_data', visibility='DUK_INTERNAL', typename='duk_uint8_t', intvalues=True, const=True, size=len(init_data))

def emit_initjs_source(genc, initjs_data):
	genc.emitLine('#if defined(DUK_USE_BUILTIN_INITJS)')
	genc.emitArray(initjs_data, 'duk_initjs_data', visibility='DUK_INTERNAL', typename='duk_uint8_t', intvalues=True, const=True, size=len(initjs_data))
	genc.emitLine('#endif  /* DUK_USE_BUILTIN_INITJS */')

def emit_ramobj_header_initjs(genc, initjs_data):
	genc.emitLine('#if defined(DUK_USE_BUILTIN_INITJS)')
	genc.emitLine('#if !defined(DUK_SINGLE_FILE)')
	genc.emitLine('DUK_INTERNAL_DECL const duk_uint8_t duk_initjs_data[%d];' % len(initjs_data))
	genc.emitLine('#endif  /* !DUK_SINGLE_FILE */')
	genc.emitDefine('DUK_BUILTIN_INITJS_DATA_LENGTH', len(initjs_data))
	genc.emitLine('#endif  /* DUK_USE_BUILTIN_INITJS */')

def emit_ramobj_header_nativefunc_array(genc, native_func_list):
	genc.emitLine('#if !defined(DUK_SINGLE_FILE)')
	genc.emitLine('DUK_INTERNAL_DECL const duk_c_function duk_bi_native_functions[%d];' % len(native_func_list))
	genc.emitLine('#endif  /* !DUK_SINGLE_FILE */')

def emit_ramobj_header_objects(genc, meta):
	objlist = meta['objects_bidx']
	for idx,o in enumerate(objlist):
		defname = 'DUK_BIDX_' + '_'.join(o['id'].upper().split('_')[1:])  # bi_foo_bar -> FOO_BAR
		genc.emitDefine(defname, idx)
	genc.emitDefine('DUK_NUM_BUILTINS', len(objlist))
	genc.emitDefine('DUK_NUM_BIDX_BUILTINS', len(objlist))                      # Objects with 'bidx'
	genc.emitDefine('DUK_NUM_ALL_BUILTINS', len(meta['objects_ram_toplevel']))  # Objects with 'bidx' + temps needed in init

def emit_ramobj_header_initdata(genc, init_data):
	genc.emitLine('#if !defined(DUK_SINGLE_FILE)')
	genc.emitLine('DUK_INTERNAL_DECL const duk_uint8_t duk_builtins_data[%d];' % len(init_data))
	genc.emitLine('#endif  /* !DUK_SINGLE_FILE */')
	genc.emitDefine('DUK_BUILTINS_DATA_LENGTH', len(init_data))

#
#  ROM init data
#
#  Compile-time initializers for ROM strings and ROM objects.  This involves
#  a lot of small details:
#
#    - Several variants are needed for different options: unpacked vs.
#      packed duk_tval, endianness, string hash in use, etc).
#
#    - Static initializers must represent objects of different size.  For
#      example, separate structs are needed for property tables of different
#      size or value typing.
#
#    - Union initializers cannot be used portable because they're only
#      available in C99 and above.
#
#    - Initializers must use 'const' correctly to ensure that the entire
#      initialization data will go into ROM (read-only data section).
#      Const pointers etc will need to be cast into non-const pointers at
#      some point to properly mix with non-const RAM pointers, so a portable
#      const losing cast is needed.
#
#    - C++ doesn't allow forward declaration of "static const" structures
#      which is problematic because there are cyclical const structures.
#

# Get string hash initializers; need to compute possible string hash variants
# which will match runtime values.
def rom_get_strhash16_macro(val):
	hash16le = dukutil.duk_heap_hashstring_dense(val, DUK__FIXED_HASH_SEED, big_endian=False, strhash16=True)
	hash16be = dukutil.duk_heap_hashstring_dense(val, DUK__FIXED_HASH_SEED, big_endian=True, strhash16=True)
	hash16sparse = dukutil.duk_heap_hashstring_sparse(val, DUK__FIXED_HASH_SEED, strhash16=True)
	return 'DUK__STRHASH16(%dU,%dU,%dU)' % (hash16le, hash16be, hash16sparse)
def rom_get_strhash32_macro(val):
	hash32le = dukutil.duk_heap_hashstring_dense(val, DUK__FIXED_HASH_SEED, big_endian=False, strhash16=False)
	hash32be = dukutil.duk_heap_hashstring_dense(val, DUK__FIXED_HASH_SEED, big_endian=True, strhash16=False)
	hash32sparse = dukutil.duk_heap_hashstring_sparse(val, DUK__FIXED_HASH_SEED, strhash16=False)
	return 'DUK__STRHASH32(%dUL,%dUL,%dUL)' % (hash32le, hash32be, hash32sparse)

# Get string character .length; must match runtime .length computation.
def rom_charlen(x):
	return dukutil.duk_unicode_unvalidated_utf8_length(x)

# Get an initializer type and initializer literal for a specified value
# (expressed in YAML metadata format).  The types and initializers depend
# on declarations emitted before the initializers, and in several cases
# use a macro to hide the selection between several initializer variants.
def rom_get_value_initializer(meta, val, bi_str_map, bi_obj_map):
	def double_bytes_initializer(val):
		# Portable and exact float initializer.
		assert(isinstance(val, str) and len(val) == 16)  # hex encoded bytes
		val = val.decode('hex')
		tmp = []
		for i in xrange(8):
			t = ord(val[i])
			if t >= 128:
				tmp.append('%dU' % t)
			else:
				tmp.append('%d' % t)
		return 'DUK__DBLBYTES(' + ','.join(tmp) + ')'

	def tval_number_initializer(val):
		return 'DUK__TVAL_NUMBER(%s)' % double_bytes_initializer(val)

	v = val['value']
	if v is None:
		init_type = 'duk_rom_tval_null'
		init_lit = 'DUK__TVAL_NULL()'
	elif isinstance(v, (bool)):
		init_type = 'duk_rom_tval_boolean'
		bval = 0
		if v:
			bval = 1
		init_lit = 'DUK__TVAL_BOOLEAN(%d)' % bval
	elif isinstance(v, (int, float)):
		fval = struct.pack('>d', float(v)).encode('hex')
		init_type = 'duk_rom_tval_number'
		init_lit = tval_number_initializer(fval)
	elif isinstance(v, (str, unicode)):
		init_type = 'duk_rom_tval_string'
		init_lit = 'DUK__TVAL_STRING(&%s)' % bi_str_map[v]
	elif isinstance(v, (dict)):
		if v['type'] == 'double':
			init_type = 'duk_rom_tval_number'
			init_lit = tval_number_initializer(v['bytes'])
		elif v['type'] == 'undefined':
			init_type = 'duk_rom_tval_undefined'
			init_lit = 'DUK__TVAL_UNDEFINED()'
		elif v['type'] == 'object':
			init_type = 'duk_rom_tval_object'
			init_lit = 'DUK__TVAL_OBJECT(&%s)' % bi_obj_map[v['id']]
		elif v['type'] == 'accessor':
			getter_object = metadata_lookup_object(meta, v['getter_id'])
			setter_object = metadata_lookup_object(meta, v['setter_id'])
			init_type = 'duk_rom_tval_accessor'
			init_lit = '{ (const duk_hobject *) &%s, (const duk_hobject *) &%s }' % (bi_obj_map[getter_object['id']], bi_obj_map[setter_object['id']])
		else:
			raise Exception('unhandled value: %r' % val)
	else:
		raise Exception('internal error: %r' % val)
	return init_type, init_lit

# Helpers to get either initializer type or value only (not both).
def rom_get_value_initializer_type(meta, val, bi_str_map, bi_obj_map):
	init_type, init_lit = rom_get_value_initializer(meta, val, bi_str_map, bi_obj_map)
	return init_type
def rom_get_value_initializer_literal(meta, val, bi_str_map, bi_obj_map):
	init_type, init_lit = rom_get_value_initializer(meta, val, bi_str_map, bi_obj_map)
	return init_lit

# Emit ROM strings source: structs/typedefs and their initializers.
# Separate initialization structs are needed for strings of different
# length.
def rom_emit_strings_source(genc, meta):
	# Write built-in strings as code section initializers.

	strs = meta['_strings_plain']  # all strings, plain versions
	reserved_words = meta['_is_plain_reserved_word']
	strict_reserved_words = meta['_is_plain_strict_reserved_word']
	strs_needing_stridx = meta['strings_stridx']

	# Sort used lengths and declare per-length initializers.
	lens = []
	for v in strs:
		strlen = len(v)
		if strlen not in lens:
			lens.append(strlen)
	lens.sort()
	for strlen in lens:
		genc.emitLine('typedef struct duk_romstr_%d duk_romstr_%d; ' % (strlen, strlen) +
		              'struct duk_romstr_%d { duk_hstring hdr; duk_uint8_t data[%d]; };' % (strlen, strlen + 1))
	genc.emitLine('')

	# String hash values depend on endianness and other factors,
	# use an initializer macro to select the appropriate hash.
	genc.emitLine('/* When unaligned access possible, 32-bit values are fetched using host order.')
	genc.emitLine(' * When unaligned access not possible, always simulate little endian order.')
	genc.emitLine(' * See: src/duk_util_hashbytes.c:duk_util_hashbytes().')
	genc.emitLine(' */')
	genc.emitLine('#if defined(DUK_USE_STRHASH_DENSE)')
	genc.emitLine('#if defined(DUK_USE_HASHBYTES_UNALIGNED_U32_ACCESS)')  # XXX: config option to be reworked
	genc.emitLine('#if defined(DUK_USE_INTEGER_BE)')
	genc.emitLine('#define DUK__STRHASH16(hash16le,hash16be,hash16sparse) (hash16be)')
	genc.emitLine('#define DUK__STRHASH32(hash32le,hash32be,hash32sparse) (hash32be)')
	genc.emitLine('#else')
	genc.emitLine('#define DUK__STRHASH16(hash16le,hash16be,hash16sparse) (hash16le)')
	genc.emitLine('#define DUK__STRHASH32(hash32le,hash32be,hash32sparse) (hash32le)')
	genc.emitLine('#endif')
	genc.emitLine('#else')
	genc.emitLine('#define DUK__STRHASH16(hash16le,hash16be,hash16sparse) (hash16le)')
	genc.emitLine('#define DUK__STRHASH32(hash32le,hash32be,hash32sparse) (hash32le)')
	genc.emitLine('#endif')
	genc.emitLine('#else  /* DUK_USE_STRHASH_DENSE */')
	genc.emitLine('#define DUK__STRHASH16(hash16le,hash16be,hash16sparse) (hash16sparse)')
	genc.emitLine('#define DUK__STRHASH32(hash32le,hash32be,hash32sparse) (hash32sparse)')
	genc.emitLine('#endif  /* DUK_USE_STRHASH_DENSE */')

	# String header initializer macro, takes into account lowmem etc.
	genc.emitLine('#if defined(DUK_USE_HEAPPTR16)')
	genc.emitLine('#if !defined(DUK_USE_REFCOUNT16)')
	genc.emitLine('#error currently assumes DUK_USE_HEAPPTR16 and DUK_USE_REFCOUNT16 are both defined')
	genc.emitLine('#endif')
	genc.emitLine('#if defined(DUK_USE_HSTRING_CLEN)')
	genc.emitLine('#define DUK__STRINIT(heaphdr_flags,refcount,hash32,hash16,blen,clen) \\')
	genc.emitLine('\t{ { (heaphdr_flags) | ((hash16) << 16), (refcount), (blen) }, (clen) }')
	genc.emitLine('#else  /* DUK_USE_HSTRING_CLEN */')
	genc.emitLine('#define DUK__STRINIT(heaphdr_flags,refcount,hash32,hash16,blen,clen) \\')
	genc.emitLine('\t{ { (heaphdr_flags) | ((hash16) << 16), (refcount), (blen) } }')
	genc.emitLine('#endif  /* DUK_USE_HSTRING_CLEN */')
	genc.emitLine('#else  /* DUK_USE_HEAPPTR16 */')
	genc.emitLine('#define DUK__STRINIT(heaphdr_flags,refcount,hash32,hash16,blen,clen) \\')
	genc.emitLine('\t{ { (heaphdr_flags), (refcount) }, (hash32), (blen), (clen) }')
	genc.emitLine('#endif  /* DUK_USE_HEAPPTR16 */')

	# Emit string initializers.
	genc.emitLine('')
	bi_str_map = {}   # string -> initializer variable name
	for str_index,v in enumerate(strs):
		bi_str_map[v] = 'duk_str_%d' % str_index

		tmp = 'DUK_INTERNAL const duk_romstr_%d duk_str_%d = {' % (len(v), str_index)
		flags = [ 'DUK_HTYPE_STRING', 'DUK_HEAPHDR_FLAG_READONLY' ]
		is_arridx = string_is_arridx(v)

		blen = len(v)
		clen = rom_charlen(v)

		if blen == clen:
			flags.append('DUK_HSTRING_FLAG_ASCII')
		if is_arridx:
			#print('%r is arridx' % v)
			flags.append('DUK_HSTRING_FLAG_ARRIDX')
		if len(v) >= 1 and v[0] == '\xff':
			flags.append('DUK_HSTRING_FLAG_INTERNAL')
		if v in [ 'eval', 'arguments' ]:
			flags.append('DUK_HSTRING_FLAG_EVAL_OR_ARGUMENTS')
		if reserved_words.has_key(v):
			flags.append('DUK_HSTRING_FLAG_RESERVED_WORD')
		if strict_reserved_words.has_key(v):
			flags.append('DUK_HSTRING_FLAG_STRICT_RESERVED_WORD')

		tmp += 'DUK__STRINIT(%s,%d,%s,%s,%d,%d),' % \
			('|'.join(flags), 1, rom_get_strhash32_macro(v), \
			 rom_get_strhash16_macro(v), blen, clen)

		tmpbytes = []
		for c in v:
			if ord(c) < 128:
				tmpbytes.append('%d' % ord(c))
			else:
				tmpbytes.append('%dU' % ord(c))
		tmpbytes.append('%d' % 0)  # NUL term
		tmp += '{' + ','.join(tmpbytes) + '}'
		tmp += '};'
		genc.emitLine(tmp)

	# Emit an array of ROM strings, used for string interning.
	#
	# XXX: String interning now simply walks through the list checking if
	# an incoming string is present in ROM.  It would be better to use
	# binary search (or perhaps even a perfect hash) for this lookup.
	# To support binary search we could emit the list in string hash
	# order, but because there are multiple different hash variants
	# there would need to be multiple lists.  We could also order the
	# strings based on the string data which is independent of the string
	# hash and still possible to binary search relatively efficiently.
	#
	# cdecl> explain const int * const foo;
	# declare foo as const pointer to const int
	genc.emitLine('')
	genc.emitLine('DUK_INTERNAL const duk_hstring * const duk_rom_strings[%d] = {'% len(strs))
	tmp = []
	linecount = 0
	for str_index,v in enumerate(strs):
		if str_index > 0:
			tmp.append(', ')
		if linecount >= 6:
			linecount = 0
			tmp.append('\n')
		tmp.append('(const duk_hstring *) &duk_str_%d' % str_index)
		linecount += 1
	for line in ''.join(tmp).split('\n'):
		genc.emitLine(line)
	genc.emitLine('};')

	# Emit an array of duk_hstring pointers indexed using DUK_STRIDX_xxx.
	# This will back e.g. DUK_HTHREAD_STRING_XYZ(thr) directly, without
	# needing an explicit array in thr/heap->strs[].
	#
	# cdecl > explain const int * const foo;
	# declare foo as const pointer to const int
	genc.emitLine('')
	genc.emitLine('DUK_INTERNAL const duk_hstring * const duk_rom_strings_stridx[%d] = {' % len(strs_needing_stridx))
	for s in strs_needing_stridx:
		genc.emitLine('\t(const duk_hstring *) &%s,' % bi_str_map[s['str']])  # strs_needing_stridx is a list of objects, not plain strings
	genc.emitLine('};')

	return bi_str_map

# Emit ROM strings header.
def rom_emit_strings_header(genc, meta):
	genc.emitLine('#if !defined(DUK_SINGLE_FILE)')  # C++ static const workaround
	genc.emitLine('DUK_INTERNAL_DECL const duk_hstring * const duk_rom_strings[%d];'% len(meta['strings']))
	genc.emitLine('DUK_INTERNAL_DECL const duk_hstring * const duk_rom_strings_stridx[%d];' % len(meta['strings_stridx']))
	genc.emitLine('#endif')

# Emit ROM objects initialized types and macros.
def rom_emit_object_initializer_types_and_macros(genc):
	# Objects and functions are straightforward because they just use the
	# RAM structure which has no dynamic or variable size parts.
	genc.emitLine('typedef struct duk_romobj duk_romobj; ' + \
	              'struct duk_romobj { duk_hobject hdr; };')
	genc.emitLine('typedef struct duk_romarr duk_romarr; ' + \
	              'struct duk_romarr { duk_harray hdr; };')
	genc.emitLine('typedef struct duk_romfun duk_romfun; ' + \
	              'struct duk_romfun { duk_hnatfunc hdr; };')

	# For ROM pointer compression we'd need a -compile time- variant.
	# The current portable solution is to just assign running numbers
	# to ROM compressed pointers, and provide the table for user pointer
	# compression function.  Much better solutions would be possible,
	# but such solutions are often compiler/platform specific.

	# Emit object/function initializer which is aware of options affecting
	# the header.  Heap next/prev pointers are always NULL.
	genc.emitLine('#if defined(DUK_USE_HEAPPTR16)')
	genc.emitLine('#if !defined(DUK_USE_REFCOUNT16) || defined(DUK_USE_HOBJECT_HASH_PART)')
	genc.emitLine('#error currently assumes DUK_USE_HEAPPTR16 and DUK_USE_REFCOUNT16 are both defined and DUK_USE_HOBJECT_HASH_PART is undefined')
	genc.emitLine('#endif')
	#genc.emitLine('#if !defined(DUK_USE_HEAPPTR_ENC16_STATIC)')
	#genc.emitLine('#error need DUK_USE_HEAPPTR_ENC16_STATIC which provides compile-time pointer compression')
	#genc.emitLine('#endif')
	genc.emitLine('#define DUK__ROMOBJ_INIT(heaphdr_flags,refcount,props,props_enc16,iproto,iproto_enc16,esize,enext,asize,hsize) \\')
	genc.emitLine('\t{ { { (heaphdr_flags), (refcount), 0, 0, (props_enc16) }, (iproto_enc16), (esize), (enext), (asize) } }')
	genc.emitLine('#define DUK__ROMARR_INIT(heaphdr_flags,refcount,props,props_enc16,iproto,iproto_enc16,esize,enext,asize,hsize,length) \\')
	genc.emitLine('\t{ { { { (heaphdr_flags), (refcount), 0, 0, (props_enc16) }, (iproto_enc16), (esize), (enext), (asize) }, (length), 0 /*length_nonwritable*/ } }')
	genc.emitLine('#define DUK__ROMFUN_INIT(heaphdr_flags,refcount,props,props_enc16,iproto,iproto_enc16,esize,enext,asize,hsize,nativefunc,nargs,magic) \\')
	genc.emitLine('\t{ { { { (heaphdr_flags), (refcount), 0, 0, (props_enc16) }, (iproto_enc16), (esize), (enext), (asize) }, (nativefunc), (duk_int16_t) (nargs), (duk_int16_t) (magic) } }')
	genc.emitLine('#else  /* DUK_USE_HEAPPTR16 */')
	genc.emitLine('#define DUK__ROMOBJ_INIT(heaphdr_flags,refcount,props,props_enc16,iproto,iproto_enc16,esize,enext,asize,hsize) \\')
	genc.emitLine('\t{ { { (heaphdr_flags), (refcount), NULL, NULL }, (duk_uint8_t *) DUK_LOSE_CONST(props), (duk_hobject *) DUK_LOSE_CONST(iproto), (esize), (enext), (asize), (hsize) } }')
	genc.emitLine('#define DUK__ROMARR_INIT(heaphdr_flags,refcount,props,props_enc16,iproto,iproto_enc16,esize,enext,asize,hsize,length) \\')
	genc.emitLine('\t{ { { { (heaphdr_flags), (refcount), NULL, NULL }, (duk_uint8_t *) DUK_LOSE_CONST(props), (duk_hobject *) DUK_LOSE_CONST(iproto), (esize), (enext), (asize), (hsize) }, (length), 0 /*length_nonwritable*/ } }')
	genc.emitLine('#define DUK__ROMFUN_INIT(heaphdr_flags,refcount,props,props_enc16,iproto,iproto_enc16,esize,enext,asize,hsize,nativefunc,nargs,magic) \\')
	genc.emitLine('\t{ { { { (heaphdr_flags), (refcount), NULL, NULL }, (duk_uint8_t *) DUK_LOSE_CONST(props), (duk_hobject *) DUK_LOSE_CONST(iproto), (esize), (enext), (asize), (hsize) }, (nativefunc), (duk_int16_t) (nargs), (duk_int16_t) (magic) } }')
	genc.emitLine('#endif  /* DUK_USE_HEAPPTR16 */')

	# Emit duk_tval structs.  This gets a bit messier with packed/unpacked
	# duk_tval, endianness variants, pointer sizes, etc.
	genc.emitLine('#if defined(DUK_USE_PACKED_TVAL)')
	genc.emitLine('typedef struct duk_rom_tval_undefined duk_rom_tval_undefined;')
	genc.emitLine('typedef struct duk_rom_tval_null duk_rom_tval_null;')
	genc.emitLine('typedef struct duk_rom_tval_boolean duk_rom_tval_boolean;')
	genc.emitLine('typedef struct duk_rom_tval_number duk_rom_tval_number;')
	genc.emitLine('typedef struct duk_rom_tval_object duk_rom_tval_object;')
	genc.emitLine('typedef struct duk_rom_tval_string duk_rom_tval_string;')
	genc.emitLine('typedef struct duk_rom_tval_accessor duk_rom_tval_accessor;')
	genc.emitLine('struct duk_rom_tval_number { duk_uint8_t bytes[8]; };')
	genc.emitLine('struct duk_rom_tval_accessor { const duk_hobject *get; const duk_hobject *set; };')
	genc.emitLine('#if defined(DUK_USE_DOUBLE_LE)')
	genc.emitLine('struct duk_rom_tval_object { const void *ptr; duk_uint32_t hiword; };')
	genc.emitLine('struct duk_rom_tval_string { const void *ptr; duk_uint32_t hiword; };')
	genc.emitLine('struct duk_rom_tval_undefined { const void *ptr; duk_uint32_t hiword; };')
	genc.emitLine('struct duk_rom_tval_null { const void *ptr; duk_uint32_t hiword; };')
	genc.emitLine('struct duk_rom_tval_boolean { duk_uint32_t dummy; duk_uint32_t hiword; };')
	genc.emitLine('#elif defined(DUK_USE_DOUBLE_BE)')
	genc.emitLine('struct duk_rom_tval_object { duk_uint32_t hiword; const void *ptr; };')
	genc.emitLine('struct duk_rom_tval_string { duk_uint32_t hiword; const void *ptr; };')
	genc.emitLine('struct duk_rom_tval_undefined { duk_uint32_t hiword; const void *ptr; };')
	genc.emitLine('struct duk_rom_tval_null { duk_uint32_t hiword; const void *ptr; };')
	genc.emitLine('struct duk_rom_tval_boolean { duk_uint32_t hiword; duk_uint32_t dummy; };')
	genc.emitLine('#elif defined(DUK_USE_DOUBLE_ME)')
	genc.emitLine('struct duk_rom_tval_object { duk_uint32_t hiword; const void *ptr; };')
	genc.emitLine('struct duk_rom_tval_string { duk_uint32_t hiword; const void *ptr; };')
	genc.emitLine('struct duk_rom_tval_undefined { duk_uint32_t hiword; const void *ptr; };')
	genc.emitLine('struct duk_rom_tval_null { duk_uint32_t hiword; const void *ptr; };')
	genc.emitLine('struct duk_rom_tval_boolean { duk_uint32_t hiword; duk_uint32_t dummy; };')
	genc.emitLine('#else')
	genc.emitLine('#error invalid endianness defines')
	genc.emitLine('#endif')
	genc.emitLine('#else  /* DUK_USE_PACKED_TVAL */')
	# Unpacked initializers are written assuming normal struct alignment
	# rules so that sizeof(duk_tval) == 16.  32-bit pointers need special
	# handling to ensure the individual initializers pad to 16 bytes as
	# necessary.
	# XXX: 32-bit unpacked duk_tval is not yet supported.
	genc.emitLine('#if defined(DUK_UINTPTR_MAX)')
	genc.emitLine('#if (DUK_UINTPTR_MAX <= 0xffffffffUL)')
	genc.emitLine('#error ROM initializer with unpacked duk_tval does not currently work on 32-bit targets')
	genc.emitLine('#endif')
	genc.emitLine('#endif')
	genc.emitLine('typedef struct duk_rom_tval_undefined duk_rom_tval_undefined;')
	genc.emitLine('struct duk_rom_tval_undefined { duk_small_uint_t tag; duk_small_uint_t extra; duk_uint8_t bytes[8]; };')
	genc.emitLine('typedef struct duk_rom_tval_null duk_rom_tval_null;')
	genc.emitLine('struct duk_rom_tval_null { duk_small_uint_t tag; duk_small_uint_t extra; duk_uint8_t bytes[8]; };')
	genc.emitLine('typedef struct duk_rom_tval_boolean duk_rom_tval_boolean;')
	genc.emitLine('struct duk_rom_tval_boolean { duk_small_uint_t tag; duk_small_uint_t extra; duk_uint32_t val; duk_uint32_t unused; };')
	genc.emitLine('typedef struct duk_rom_tval_number duk_rom_tval_number;')
	genc.emitLine('struct duk_rom_tval_number { duk_small_uint_t tag; duk_small_uint_t extra; duk_uint8_t bytes[8]; };')
	genc.emitLine('typedef struct duk_rom_tval_object duk_rom_tval_object;')
	genc.emitLine('struct duk_rom_tval_object { duk_small_uint_t tag; duk_small_uint_t extra; const duk_heaphdr *val; };')
	genc.emitLine('typedef struct duk_rom_tval_string duk_rom_tval_string;')
	genc.emitLine('struct duk_rom_tval_string { duk_small_uint_t tag; duk_small_uint_t extra; const duk_heaphdr *val; };')
	genc.emitLine('typedef struct duk_rom_tval_accessor duk_rom_tval_accessor;')
	genc.emitLine('struct duk_rom_tval_accessor { const duk_hobject *get; const duk_hobject *set; };')
	genc.emitLine('#endif  /* DUK_USE_PACKED_TVAL */')
	genc.emitLine('')

	# Double initializer byte shuffle macro to handle byte orders
	# without duplicating the entire initializers.
	genc.emitLine('#if defined(DUK_USE_DOUBLE_LE)')
	genc.emitLine('#define DUK__DBLBYTES(a,b,c,d,e,f,g,h) { (h), (g), (f), (e), (d), (c), (b), (a) }')
	genc.emitLine('#elif defined(DUK_USE_DOUBLE_BE)')
	genc.emitLine('#define DUK__DBLBYTES(a,b,c,d,e,f,g,h) { (a), (b), (c), (d), (e), (f), (g), (h) }')
	genc.emitLine('#elif defined(DUK_USE_DOUBLE_ME)')
	genc.emitLine('#define DUK__DBLBYTES(a,b,c,d,e,f,g,h) { (d), (c), (b), (a), (h), (g), (f), (e) }')
	genc.emitLine('#else')
	genc.emitLine('#error invalid endianness defines')
	genc.emitLine('#endif')
	genc.emitLine('')

	# Emit duk_tval initializer literal macros.
	genc.emitLine('#if defined(DUK_USE_PACKED_TVAL)')
	genc.emitLine('#define DUK__TVAL_NUMBER(hostbytes) { hostbytes }')  # bytes already in host order
	genc.emitLine('#if defined(DUK_USE_DOUBLE_LE)')
	genc.emitLine('#define DUK__TVAL_UNDEFINED() { (const void *) NULL, (DUK_TAG_UNDEFINED << 16) }')
	genc.emitLine('#define DUK__TVAL_NULL() { (const void *) NULL, (DUK_TAG_NULL << 16) }')
	genc.emitLine('#define DUK__TVAL_BOOLEAN(bval) { 0, (DUK_TAG_BOOLEAN << 16) + (bval) }')
	genc.emitLine('#define DUK__TVAL_OBJECT(ptr) { (const void *) (ptr), (DUK_TAG_OBJECT << 16) }')
	genc.emitLine('#define DUK__TVAL_STRING(ptr) { (const void *) (ptr), (DUK_TAG_STRING << 16) }')
	genc.emitLine('#elif defined(DUK_USE_DOUBLE_BE)')
	genc.emitLine('#define DUK__TVAL_UNDEFINED() { (DUK_TAG_UNDEFINED << 16), (const void *) NULL }')
	genc.emitLine('#define DUK__TVAL_NULL() { (DUK_TAG_NULL << 16), (const void *) NULL }')
	genc.emitLine('#define DUK__TVAL_BOOLEAN(bval) { (DUK_TAG_BOOLEAN << 16) + (bval), 0 }')
	genc.emitLine('#define DUK__TVAL_OBJECT(ptr) { (DUK_TAG_OBJECT << 16), (const void *) (ptr) }')
	genc.emitLine('#define DUK__TVAL_STRING(ptr) { (DUK_TAG_STRING << 16), (const void *) (ptr) }')
	genc.emitLine('#elif defined(DUK_USE_DOUBLE_ME)')
	genc.emitLine('#define DUK__TVAL_UNDEFINED() { (DUK_TAG_UNDEFINED << 16), (const void *) NULL }')
	genc.emitLine('#define DUK__TVAL_NULL() { (DUK_TAG_NULL << 16), (const void *) NULL }')
	genc.emitLine('#define DUK__TVAL_BOOLEAN(bval) { (DUK_TAG_BOOLEAN << 16) + (bval), 0 }')
	genc.emitLine('#define DUK__TVAL_OBJECT(ptr) { (DUK_TAG_OBJECT << 16), (const void *) (ptr) }')
	genc.emitLine('#define DUK__TVAL_STRING(ptr) { (DUK_TAG_STRING << 16), (const void *) (ptr) }')
	genc.emitLine('#else')
	genc.emitLine('#error invalid endianness defines')
	genc.emitLine('#endif')
	genc.emitLine('#else  /* DUK_USE_PACKED_TVAL */')
	genc.emitLine('#define DUK__TVAL_NUMBER(hostbytes) { DUK__TAG_NUMBER, 0, hostbytes }')  # bytes already in host order
	genc.emitLine('#define DUK__TVAL_UNDEFINED() { DUK_TAG_UNDEFINED, 0, {0,0,0,0,0,0,0,0} }')
	genc.emitLine('#define DUK__TVAL_NULL() { DUK_TAG_NULL, 0, {0,0,0,0,0,0,0,0} }')
	genc.emitLine('#define DUK__TVAL_BOOLEAN(bval) { DUK_TAG_BOOLEAN, 0, (bval), 0 }')
	genc.emitLine('#define DUK__TVAL_OBJECT(ptr) { DUK_TAG_OBJECT, 0, (const duk_heaphdr *) (ptr) }')
	genc.emitLine('#define DUK__TVAL_STRING(ptr) { DUK_TAG_STRING, 0, (const duk_heaphdr *) (ptr) }')
	genc.emitLine('#endif  /* DUK_USE_PACKED_TVAL */')

# Emit ROM objects source: the object/function headers themselves, property
# table structs for different property table sizes/types, and property table
# initializers.
def rom_emit_objects(genc, meta, bi_str_map):
	objs = meta['objects']
	id_to_bidx = meta['_objid_to_bidx']

	# Table for compressed ROM pointers; reserve high range of compressed pointer
	# values for this purpose.  This must contain all ROM pointers that might be
	# referenced (all objects, strings, and property tables at least).
	romptr_compress_list = []
	def compress_rom_ptr(x):
		if x == 'NULL':
			return 0
		try:
			idx = romptr_compress_list.index(x)
			res = ROMPTR_FIRST + idx
		except ValueError:
			romptr_compress_list.append(x)
			res = ROMPTR_FIRST + len(romptr_compress_list) - 1
		assert(res <= 0xffff)
		return res

	# Need string and object maps (id -> C symbol name) early.
	bi_obj_map = {}   # object id -> initializer variable name
	for idx,obj in enumerate(objs):
		bi_obj_map[obj['id']] = 'duk_obj_%d' % idx

	# Add built-in strings and objects to compressed ROM pointers first.
	for k in sorted(bi_str_map.keys()):
		compress_rom_ptr('&%s' % bi_str_map[k])
	for k in sorted(bi_obj_map.keys()):
		compress_rom_ptr('&%s' % bi_obj_map[k])

	# Property attributes lookup, map metadata attribute string into a
	# C initializer.
	attr_lookup = {
		'':	'DUK_PROPDESC_FLAGS_NONE',
		'w':	'DUK_PROPDESC_FLAGS_W',
		'e':	'DUK_PROPDESC_FLAGS_E',
		'c':	'DUK_PROPDESC_FLAGS_C',
		'we':	'DUK_PROPDESC_FLAGS_WE',
		'wc':	'DUK_PROPDESC_FLAGS_WC',
		'ec':	'DUK_PROPDESC_FLAGS_EC',
		'wec':	'DUK_PROPDESC_FLAGS_WEC',
		'a':	'DUK_PROPDESC_FLAGS_NONE|DUK_PROPDESC_FLAG_ACCESSOR',
		'ea':	'DUK_PROPDESC_FLAGS_E|DUK_PROPDESC_FLAG_ACCESSOR',
		'ca':	'DUK_PROPDESC_FLAGS_C|DUK_PROPDESC_FLAG_ACCESSOR',
		'eca':	'DUK_PROPDESC_FLAGS_EC|DUK_PROPDESC_FLAG_ACCESSOR',
	}

	# Emit property table structs.  These are very complex because
	# property count *and* individual property type affect the fields
	# in the initializer, properties can be data properties or accessor
	# properties or different duk_tval types.  There are also several
	# property table memory layouts, each with a different ordering of
	# keys, values, etc.  Union initializers would make things a bit
	# easier but they're not very portable (being C99).
	#
	# The easy solution is to use a separate initializer type for each
	# property type.  Could also cache and reuse identical initializers
	# but there'd be very few of them so it's more straightforward to
	# not reuse the structs.
	#
	# NOTE: naming is a bit inconsistent here, duk_tval is used also
	# to refer to property value initializers like a getter/setter pair.

	genc.emitLine('#if defined(DUK_USE_HOBJECT_LAYOUT_1)')
	for idx,obj in enumerate(objs):
		numprops = len(obj['properties'])
		if numprops == 0:
			continue
		tmp = 'typedef struct duk_romprops_%d duk_romprops_%d; ' % (idx, idx)
		tmp += 'struct duk_romprops_%d { ' % idx
		for idx,val in enumerate(obj['properties']):
			tmp += 'const duk_hstring *key%d; ' % idx
		for idx,val in enumerate(obj['properties']):
			# XXX: fastint support
			tmp += '%s val%d; ' % (rom_get_value_initializer_type(meta, val, bi_str_map, bi_obj_map), idx)
		for idx,val in enumerate(obj['properties']):
			tmp += 'duk_uint8_t flags%d; ' % idx
		tmp += '};'
		genc.emitLine(tmp)
	genc.emitLine('#elif defined(DUK_USE_HOBJECT_LAYOUT_2)')
	for idx,obj in enumerate(objs):
		numprops = len(obj['properties'])
		if numprops == 0:
			continue
		tmp = 'typedef struct duk_romprops_%d duk_romprops_%d; ' % (idx, idx)
		tmp += 'struct duk_romprops_%d { ' % idx
		for idx,val in enumerate(obj['properties']):
			# XXX: fastint support
			tmp += '%s val%d; ' % (rom_get_value_initializer_type(meta, val, bi_str_map, bi_obj_map), idx)
		for idx,val in enumerate(obj['properties']):
			tmp += 'const duk_hstring *key%d; ' % idx
		for idx,val in enumerate(obj['properties']):
			tmp += 'duk_uint8_t flags%d; ' % idx
		# Padding follows for flags, but we don't need to emit it
		# (at the moment there is never an array or hash part).
		tmp += '};'
		genc.emitLine(tmp)
	genc.emitLine('#elif defined(DUK_USE_HOBJECT_LAYOUT_3)')
	for idx,obj in enumerate(objs):
		numprops = len(obj['properties'])
		if numprops == 0:
			continue
		tmp = 'typedef struct duk_romprops_%d duk_romprops_%d; ' % (idx, idx)
		tmp += 'struct duk_romprops_%d { ' % idx
		for idx,val in enumerate(obj['properties']):
			# XXX: fastint support
			tmp += '%s val%d; ' % (rom_get_value_initializer_type(meta, val, bi_str_map, bi_obj_map), idx)
		# No array values
		for idx,val in enumerate(obj['properties']):
			tmp += 'const duk_hstring *key%d; ' % idx
		# No hash index
		for idx,val in enumerate(obj['properties']):
			tmp += 'duk_uint8_t flags%d; ' % idx
		tmp += '};'
		genc.emitLine(tmp)
	genc.emitLine('#else')
	genc.emitLine('#error invalid object layout')
	genc.emitLine('#endif')
	genc.emitLine('')

	# Forward declare all property tables so that objects can reference them.
	# Also pointer compress them.

	for idx,obj in enumerate(objs):
		numprops = len(obj['properties'])
		if numprops == 0:
			continue

		# We would like to use DUK_INTERNAL_DECL here, but that maps
		# to "static const" in a single file build which has C++
		# portability issues: you can't forward declare a static const.
		# We can't reorder the property tables to avoid this because
		# there are cyclic references.  So, as the current workaround,
		# declare as external.
		genc.emitLine('DUK_EXTERNAL_DECL const duk_romprops_%d duk_prop_%d;' % (idx, idx))

		# Add property tables to ROM compressed pointers too.
		compress_rom_ptr('&duk_prop_%d' % idx)
	genc.emitLine('')

	# Forward declare all objects so that objects can reference them,
	# e.g. internal prototype reference.

	for idx,obj in enumerate(objs):
		# Careful with C++: must avoid redefining a non-extern const.
		# See commentary above for duk_prop_%d forward declarations.
		if obj.get('callable', False):
			genc.emitLine('DUK_EXTERNAL_DECL const duk_romfun duk_obj_%d;' % idx)
		elif obj.get('class') == 'Array':
			genc.emitLine('DUK_EXTERNAL_DECL const duk_romarr duk_obj_%d;' % idx)
		else:
			genc.emitLine('DUK_EXTERNAL_DECL const duk_romobj duk_obj_%d;' % idx)
	genc.emitLine('')

	# Define objects, reference property tables.  Objects will be
	# logically non-extensible so also leave their extensible flag
	# cleared despite what metadata requests; the runtime code expects
	# ROM objects to be non-extensible.
	for idx,obj in enumerate(objs):
		numprops = len(obj['properties'])

		isfunc = obj.get('callable', False)

		if isfunc:
			tmp = 'DUK_EXTERNAL const duk_romfun duk_obj_%d = ' % idx
		elif obj.get('class') == 'Array':
			tmp = 'DUK_EXTERNAL const duk_romarr duk_obj_%d = ' % idx
		else:
			tmp = 'DUK_EXTERNAL const duk_romobj duk_obj_%d = ' % idx

		flags = [ 'DUK_HTYPE_OBJECT', 'DUK_HEAPHDR_FLAG_READONLY' ]
		if isfunc:
			flags.append('DUK_HOBJECT_FLAG_NATFUNC')
			flags.append('DUK_HOBJECT_FLAG_STRICT')
			flags.append('DUK_HOBJECT_FLAG_NEWENV')
		if obj.get('constructable', False):
			flags.append('DUK_HOBJECT_FLAG_CONSTRUCTABLE')
		if obj.get('class') == 'Array':
			flags.append('DUK_HOBJECT_FLAG_EXOTIC_ARRAY')
		flags.append('DUK_HOBJECT_CLASS_AS_FLAGS(%d)' % class_to_number(obj['class']))  # XXX: use constant, not number

		refcount = 1  # refcount is faked to be always 1
		if numprops == 0:
			props = 'NULL'
		else:
			props = '&duk_prop_%d' % idx
		props_enc16 = compress_rom_ptr(props)

		if obj.has_key('internal_prototype'):
			iproto = '&%s' % bi_obj_map[obj['internal_prototype']]
		else:
			iproto = 'NULL'
		iproto_enc16 = compress_rom_ptr(iproto)

		e_size = numprops
		e_next = e_size
		a_size = 0  # never an array part for now
		h_size = 0  # never a hash for now; not appropriate for perf relevant builds

		if isfunc:
			nativefunc = obj['native']
			if obj.get('varargs', False):
				nargs = 'DUK_VARARGS'
			elif obj.has_key('nargs'):
				nargs = '%d' % obj['nargs']
			else:
				assert(False)  # 'nargs' should be defaulted from 'length' at metadata load
			magic = '%d' % resolve_magic(obj.get('magic', None), id_to_bidx)
		else:
			nativefunc = 'dummy'
			nargs = '0'
			magic = '0'

		assert(a_size == 0)
		assert(h_size == 0)
		if isfunc:
			tmp += 'DUK__ROMFUN_INIT(%s,%d,%s,%d,%s,%d,%d,%d,%d,%d,%s,%s,%s);' % \
				('|'.join(flags), refcount, props, props_enc16, \
				 iproto, iproto_enc16, e_size, e_next, a_size, h_size, \
				 nativefunc, nargs, magic)
		elif obj.get('class') == 'Array':
			arrlen = 0
			tmp += 'DUK__ROMARR_INIT(%s,%d,%s,%d,%s,%d,%d,%d,%d,%d,%d);' % \
				('|'.join(flags), refcount, props, props_enc16, \
				 iproto, iproto_enc16, e_size, e_next, a_size, h_size, arrlen)
		else:
			tmp += 'DUK__ROMOBJ_INIT(%s,%d,%s,%d,%s,%d,%d,%d,%d,%d);' % \
				('|'.join(flags), refcount, props, props_enc16, \
				 iproto, iproto_enc16, e_size, e_next, a_size, h_size)

		genc.emitLine(tmp)

	# Property tables.  Can reference arbitrary strings and objects as
	# they're defined before them.

	# Properties will be non-configurable, but must be writable so that
	# standard property semantics allow shadowing properties to be
	# established in inherited objects (e.g. "var obj={}; obj.toString
	# = myToString").  Enumerable can also be kept.

	def _prepAttrs(val):
		attrs = val['attributes']
		assert('c' not in attrs)
		return attr_lookup[attrs]

	def _emitPropTableInitializer(idx, obj, layout):
		init_vals = []
		init_keys = []
		init_flags = []

		numprops = len(obj['properties'])
		for val in obj['properties']:
			init_keys.append('(const duk_hstring *)&%s' % bi_str_map[val['key']])
		for val in obj['properties']:
			# XXX: fastint support
			init_vals.append('%s' % rom_get_value_initializer_literal(meta, val, bi_str_map, bi_obj_map))
		for val in obj['properties']:
			init_flags.append('%s' % _prepAttrs(val))

		if layout == 1:
			initlist = init_keys + init_vals + init_flags
		elif layout == 2:
			initlist = init_vals + init_keys + init_flags
		elif layout == 3:
			# Same as layout 2 now, no hash/array
			initlist = init_vals + init_keys + init_flags

		if len(initlist) > 0:
			genc.emitLine('DUK_EXTERNAL const duk_romprops_%d duk_prop_%d = {%s};' % (idx, idx, ','.join(initlist)))

	genc.emitLine('#if defined(DUK_USE_HOBJECT_LAYOUT_1)')
	for idx,obj in enumerate(objs):
		_emitPropTableInitializer(idx, obj, 1)
	genc.emitLine('#elif defined(DUK_USE_HOBJECT_LAYOUT_2)')
	for idx,obj in enumerate(objs):
		_emitPropTableInitializer(idx, obj, 2)
	genc.emitLine('#elif defined(DUK_USE_HOBJECT_LAYOUT_3)')
	for idx,obj in enumerate(objs):
		_emitPropTableInitializer(idx, obj, 3)
	genc.emitLine('#else')
	genc.emitLine('#error invalid object layout')
	genc.emitLine('#endif')
	genc.emitLine('')

	# Emit a list of ROM builtins (those objects needing a bidx).
	#
	# cdecl > explain const int * const foo;
	# declare foo as const pointer to const int

	count_bidx = 0
	for bi in objs:
		if bi.get('bidx_used', False):
			count_bidx += 1
	genc.emitLine('DUK_INTERNAL const duk_hobject * const duk_rom_builtins_bidx[%d] = {' % count_bidx)
	for bi in objs:
		if not bi.get('bidx_used', False):
			continue  # for this we want the toplevel objects only
		genc.emitLine('\t(const duk_hobject *) &%s,' % bi_obj_map[bi['id']])
	genc.emitLine('};')

	# Emit a table of compressed ROM pointers.  We must be able to
	# compress ROM pointers at compile time so we assign running
	# indices to them.  User pointer compression macros must use this
	# array to encode/decode ROM pointers.

	genc.emitLine('')
	genc.emitLine('#if defined(DUK_USE_ROM_OBJECTS) && defined(DUK_USE_HEAPPTR16)')
	genc.emitLine('DUK_EXTERNAL const void * const duk_rom_compressed_pointers[%d] = {' % (len(romptr_compress_list) + 1))
	for idx,ptr in enumerate(romptr_compress_list):
		genc.emitLine('\t(const void *) %s,  /* 0x%04x */' % (ptr, ROMPTR_FIRST + idx))
	romptr_highest = ROMPTR_FIRST + len(romptr_compress_list) - 1
	genc.emitLine('\tNULL')  # for convenience
	genc.emitLine('};')
	genc.emitLine('#endif')

	print('%d compressed rom pointers (used range is [0x%04x,0x%04x], %d space left)' % \
	      (len(romptr_compress_list), ROMPTR_FIRST, romptr_highest, 0xffff - romptr_highest))

	# Undefine helpers.
	genc.emitLine('')
	for i in [
		'DUK__STRHASH16',
		'DUK__STRHASH32',
		'DUK__DBLBYTES',
		'DUK__TVAL_NUMBER',
		'DUK__TVAL_UNDEFINED',
		'DUK__TVAL_NULL',
		'DUK__TVAL_BOOLEAN',
		'DUK__TVAL_OBJECT',
		'DUK__TVAL_STRING',
		'DUK__STRINIT',
		'DUK__ROMOBJ_INIT',
		'DUK__ROMFUN_INIT'
	]:
		genc.emitLine('#undef ' + i)

	return romptr_compress_list

# Emit ROM objects header.
def rom_emit_objects_header(genc, meta):
	bidx = 0
	for bi in meta['objects']:
		if not bi.get('bidx_used', False):
			continue  # for this we want the toplevel objects only
		genc.emitDefine('DUK_BIDX_' + '_'.join(bi['id'].upper().split('_')[1:]), bidx)  # bi_foo_bar -> FOO_BAR
		bidx += 1
	count_bidx = bidx
	genc.emitDefine('DUK_NUM_BUILTINS', count_bidx)
	genc.emitDefine('DUK_NUM_BIDX_BUILTINS', count_bidx)
	genc.emitDefine('DUK_NUM_ALL_BUILTINS', len(meta['objects']))
	genc.emitLine('')
	genc.emitLine('#if !defined(DUK_SINGLE_FILE)')  # C++ static const workaround
	genc.emitLine('DUK_INTERNAL_DECL const duk_hobject * const duk_rom_builtins_bidx[%d];' % count_bidx)
	genc.emitLine('#endif')

	# XXX: missing declarations here, not an issue for single source build.
	# Add missing declarations.
	# XXX: For example, 'DUK_EXTERNAL_DECL ... duk_rom_compressed_pointers[]' is missing.

#
#  Shared for both RAM and ROM
#

def emit_header_native_function_declarations(genc, meta):
	emitted = {}  # To suppress duplicates
	for o in meta['objects']:
		if not o.has_key('native'):
			continue
		fname = o['native']
		if emitted.has_key(fname):
			continue  # already emitted, suppress duplicate
		emitted[fname] = True

		# Visibility depends on whether the function is Duktape internal or user.
		# Use a simple prefix for now.
		if fname[:4] == 'duk_':
			genc.emitLine('DUK_INTERNAL_DECL duk_ret_t %s(duk_context *ctx);' % o['native'])
		else:
			genc.emitLine('extern duk_ret_t %s(duk_context *ctx);' % o['native'])

#
#  Main
#

def main():
	parser = optparse.OptionParser()
	parser.add_option('--buildinfo', dest='buildinfo', help='Build info, JSON format')
	parser.add_option('--initjs-data', dest='initjs_data', help='InitJS data to embed')
	parser.add_option('--used-stridx-metadata', dest='used_stridx_metadata', help='DUK_STRIDX_xxx used by source/headers, JSON format')
	parser.add_option('--strings-metadata', dest='strings_metadata', help='Built-in strings metadata file, YAML format')
	parser.add_option('--objects-metadata', dest='objects_metadata', help='Built-in objects metadata file, YAML format')
	parser.add_option('--user-builtin-metadata', dest='user_builtin_metadata', action='append', default=[], help='User strings and objects to add, YAML format (can be repeated for multiple overrides)')
	parser.add_option('--rom-support', dest='rom_support', action='store_true', default=False, help='Support ROM strings/objects (increases output size considerably)')
	parser.add_option('--out-header', dest='out_header', help='Output header file')
	parser.add_option('--out-source', dest='out_source', help='Output source file')
	parser.add_option('--out-metadata-json', dest='out_metadata_json', help='Output metadata file')
	parser.add_option('--dev-dump-final-ram-metadata', dest='dev_dump_final_ram_metadata', help='Development option')
	parser.add_option('--dev-dump-final-rom-metadata', dest='dev_dump_final_rom_metadata', help='Development option')
	(opts, args) = parser.parse_args()

	# Options processing.

	if opts.buildinfo is None:
		raise Exception('missing buildinfo')

	with open(opts.buildinfo, 'rb') as f:
		build_info = dukutil.json_decode(f.read().strip())

	if opts.initjs_data is None:
		initjs_data = ''
	else:
		with open(opts.initjs_data, 'rb') as f:
			initjs_data = f.read()
		if len(initjs_data) > 1 and initjs_data[-1] != '\0':
			# force NUL termination, init code now expects that
			initjs_data += '\0'

	# Read in metadata files, normalizing and merging as necessary.

	ram_meta = load_metadata(opts, rom=False, build_info=build_info)
	rom_meta = load_metadata(opts, rom=True, build_info=build_info)
	if opts.dev_dump_final_ram_metadata is not None:
		dump_metadata(ram_meta, opts.dev_dump_final_ram_metadata)
	if opts.dev_dump_final_rom_metadata is not None:
		dump_metadata(rom_meta, opts.dev_dump_final_rom_metadata)

	# Create RAM init data bitstreams.

	ramstr_data, ramstr_maxlen = gen_ramstr_initdata_bitpacked(ram_meta)
	ram_native_funcs, ram_natfunc_name_to_natidx = get_ramobj_native_func_maps(ram_meta)

	ramobj_data_le = gen_ramobj_initdata_bitpacked(ram_meta, ram_native_funcs, ram_natfunc_name_to_natidx, 'little')
	ramobj_data_be = gen_ramobj_initdata_bitpacked(ram_meta, ram_native_funcs, ram_natfunc_name_to_natidx, 'big')
	ramobj_data_me = gen_ramobj_initdata_bitpacked(ram_meta, ram_native_funcs, ram_natfunc_name_to_natidx, 'mixed')

	# Write source and header files.

	gc_src = dukutil.GenerateC()
	gc_src.emitHeader('genbuiltins.py')
	gc_src.emitLine('#include "duk_internal.h"')
	gc_src.emitLine('')
	gc_src.emitLine('#if defined(DUK_USE_ROM_STRINGS)')
	if opts.rom_support:
		rom_bi_str_map = rom_emit_strings_source(gc_src, rom_meta)
		rom_emit_object_initializer_types_and_macros(gc_src)
		rom_emit_objects(gc_src, rom_meta, rom_bi_str_map)
	else:
		gc_src.emitLine('#error ROM support not enabled, rerun make_dist.py with --rom-support')
	gc_src.emitLine('#else  /* DUK_USE_ROM_STRINGS */')
	emit_ramstr_source_strinit_data(gc_src, ramstr_data)
	gc_src.emitLine('#endif  /* DUK_USE_ROM_STRINGS */')
	gc_src.emitLine('')
	gc_src.emitLine('#if defined(DUK_USE_ROM_OBJECTS)')
	if opts.rom_support:
		gc_src.emitLine('#if !defined(DUK_USE_ROM_STRINGS)')
		gc_src.emitLine('#error DUK_USE_ROM_OBJECTS requires DUK_USE_ROM_STRINGS')
		gc_src.emitLine('#endif')
	else:
		gc_src.emitLine('#error ROM support not enabled, rerun make_dist.py with --rom-support')
	gc_src.emitLine('#else  /* DUK_USE_ROM_OBJECTS */')
	emit_ramobj_source_nativefunc_array(gc_src, ram_native_funcs)  # endian independent
	emit_initjs_source(gc_src, initjs_data)  # InitJS is now only active with RAM objects
	gc_src.emitLine('#if defined(DUK_USE_DOUBLE_LE)')
	emit_ramobj_source_objinit_data(gc_src, ramobj_data_le)
	gc_src.emitLine('#elif defined(DUK_USE_DOUBLE_BE)')
	emit_ramobj_source_objinit_data(gc_src, ramobj_data_be)
	gc_src.emitLine('#elif defined(DUK_USE_DOUBLE_ME)')
	emit_ramobj_source_objinit_data(gc_src, ramobj_data_me)
	gc_src.emitLine('#else')
	gc_src.emitLine('#error invalid endianness defines')
	gc_src.emitLine('#endif')
	gc_src.emitLine('#endif  /* DUK_USE_ROM_OBJECTS */')

	gc_hdr = dukutil.GenerateC()
	gc_hdr.emitHeader('genbuiltins.py')
	gc_hdr.emitLine('#ifndef DUK_BUILTINS_H_INCLUDED')
	gc_hdr.emitLine('#define DUK_BUILTINS_H_INCLUDED')
	gc_hdr.emitLine('')
	gc_hdr.emitLine('#if defined(DUK_USE_ROM_STRINGS)')
	if opts.rom_support:
		emit_header_stridx_defines(gc_hdr, rom_meta)
		rom_emit_strings_header(gc_hdr, rom_meta)
	else:
		gc_hdr.emitLine('#error ROM support not enabled, rerun make_dist.py with --rom-support')
	gc_hdr.emitLine('#else  /* DUK_USE_ROM_STRINGS */')
	emit_header_stridx_defines(gc_hdr, ram_meta)
	emit_ramstr_header_strinit_defines(gc_hdr, ram_meta, ramstr_data, ramstr_maxlen)
	gc_hdr.emitLine('#endif  /* DUK_USE_ROM_STRINGS */')
	gc_hdr.emitLine('')
	gc_hdr.emitLine('#if defined(DUK_USE_ROM_OBJECTS)')
	if opts.rom_support:
		# Currently DUK_USE_ROM_PTRCOMP_FIRST must match our fixed
		# define, and the two must be updated in sync.  Catch any
		# mismatch to avoid difficult to diagnose errors.
		gc_hdr.emitLine('#if !defined(DUK_USE_ROM_PTRCOMP_FIRST)')
		gc_hdr.emitLine('#error missing DUK_USE_ROM_PTRCOMP_FIRST define')
		gc_hdr.emitLine('#endif')
		gc_hdr.emitLine('#if (DUK_USE_ROM_PTRCOMP_FIRST != %dL)' % ROMPTR_FIRST)
		gc_hdr.emitLine('#error DUK_USE_ROM_PTRCOMP_FIRST must match ROMPTR_FIRST in genbuiltins.py (%d), update manually and re-dist' % ROMPTR_FIRST)
		gc_hdr.emitLine('#endif')
		emit_header_native_function_declarations(gc_hdr, rom_meta)
		rom_emit_objects_header(gc_hdr, rom_meta)
	else:
		gc_hdr.emitLine('#error ROM support not enabled, rerun make_dist.py with --rom-support')
	gc_hdr.emitLine('#else')
	emit_header_native_function_declarations(gc_hdr, rom_meta)
	emit_ramobj_header_nativefunc_array(gc_hdr, ram_native_funcs)
	emit_ramobj_header_initjs(gc_hdr, initjs_data)
	emit_ramobj_header_objects(gc_hdr, ram_meta)
	gc_hdr.emitLine('#if defined(DUK_USE_DOUBLE_LE)')
	emit_ramobj_header_initdata(gc_hdr, ramobj_data_le)
	gc_hdr.emitLine('#elif defined(DUK_USE_DOUBLE_BE)')
	emit_ramobj_header_initdata(gc_hdr, ramobj_data_be)
	gc_hdr.emitLine('#elif defined(DUK_USE_DOUBLE_ME)')
	emit_ramobj_header_initdata(gc_hdr, ramobj_data_me)
	gc_hdr.emitLine('#else')
	gc_hdr.emitLine('#error invalid endianness defines')
	gc_hdr.emitLine('#endif')
	gc_hdr.emitLine('#endif  /* DUK_USE_ROM_OBJECTS */')
	gc_hdr.emitLine('#endif  /* DUK_BUILTINS_H_INCLUDED */')

	with open(opts.out_source, 'wb') as f:
		f.write(gc_src.getString())

	with open(opts.out_header, 'wb') as f:
		f.write(gc_hdr.getString())

	# Write a JSON file with build metadata, e.g. built-in strings.

	ver = long(build_info['version'])
	plain_strs = []
	base64_strs = []
	str_objs = []
	for s in ram_meta['strings_stridx']:  # XXX: provide all lists?
		t1 = bytes_to_unicode(s['str'])
		t2 = unicode_to_bytes(s['str']).encode('base64').strip()
		plain_strs.append(t1)
		base64_strs.append(t2)
		str_objs.append({
			'plain': t1, 'base64': t2, 'define': s['define']
		})
	meta = {
		'comment': 'Metadata for Duktape build',
		'duk_version': ver,
		'duk_version_string': '%d.%d.%d' % (ver / 10000, (ver / 100) % 100, ver % 100),
		'git_describe': build_info['git_describe'],
		'builtin_strings': plain_strs,
		'builtin_strings_base64': base64_strs,
		'builtin_strings_info': str_objs
	}

	with open(opts.out_metadata_json, 'wb') as f:
		f.write(json.dumps(meta, indent=4, sort_keys=True, ensure_ascii=True))

if __name__ == '__main__':
	main()
