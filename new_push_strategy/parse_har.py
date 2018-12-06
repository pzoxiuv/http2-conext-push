from dateutil import parser
import datetime
import json
import sys
import numpy
import urlparse
import argparse
import os

def create_push_config(trigger_req, pushed_objs, added_objs):
	push_config = {}
	push_config['push_host'] = urlparse.urlparse(trigger_req).hostname
	push_config['push_trigger'] = urlparse.urlparse(trigger_req).path
	push_config['push_resources'] = []
	for obj in pushed_objs:
		if obj not in added_objs:
			push_config['push_resources'].append(obj)
	return push_config

def write_output(deps, parent_times, output_file, template_file, hostname, hostname_dir_file):
	with open(hostname_dir_file) as f:
		dir_mapping = json.load(f)

	cf='../../../html/{}/captured.html'.format(dir_mapping[hostname])
	ccf='../../../css/{}/critical.css'.format(dir_mapping[hostname])
	with open (template_file) as f:	
		output = json.load(f)
	output['h2o_custom_scheduler'][0]['hostname'] = hostname
	output['h2o_custom_scheduler'][0]['content_file'] = cf
	output['h2o_custom_scheduler'][0]['critical_css_file'] = ccf

	# We can only push each object once, so only output each kid once
	added_objs = set()

	# force index.html to be first:
	if 'http://'+hostname+'/' in deps:
		index = 'http://'+hostname+'/'
	elif 'https://'+hostname+'/':
		index = 'https://'+hostname+'/'
	if index != None and index in deps:
		output['push_configs'].append(create_push_config(index, deps[index], added_objs))
		added_objs.update(output['push_configs'][-1]['push_resources'])

	added_obs = []
	for trigger_req, _ in sorted(parent_times.items(), key=lambda x: x[1]):
		if trigger_req == index:
			continue
		pc = create_push_config(trigger_req, deps[trigger_req], added_objs)
		if len(pc['push_resources']) > 0:
			output['push_configs'].append(pc)
			added_objs.update(pc['push_resources'])

	with open(output_file, 'w') as f:
		json.dump(output, f, indent=4)

def get_gap_objs(l, i, mergelist_file, outlier_gap):
	"""
	l is a list of (req. start time, object name, time delta) tuples from a page load
	i is the index in that list where we found a gap
	mergefile_list says what hosts we can push objects from (the originally requested host
		plus hosts the original host is authoritative for)
	outlier_gap is the length of time gap that we consider to be likely an RTT
	"""

	merged_hosts = []
	with open(mergelist_file) as f:
		for host in f:
			merged_hosts.append(host.strip())

	# Iterates backward through object list until it finds a time gap as long as
	# our suspected RTT.  Each object is a parent in a possible dependency pair
	pairs = []
	for x in xrange(i-1, -1, -1):
		if x < 0:
			break
		if l[x][2] > outlier_gap:
			break
		# Iterates forward through object list until it finds a time gap as long as
		# our suspected RTT.  Each object is the child of the current parent in
		# a possible dependency pair.
		for y in xrange(i, len(l)):
			if l[y][2] > outlier_gap and y > i:
				break
			# skip assets not hosted locally
			h1 = urlparse.urlparse(l[x][1]).hostname
			h2 = urlparse.urlparse(l[y][1]).hostname
			if (l[x][1], l[y][1]) not in pairs and h1 in merged_hosts and h2 in merged_hosts:
				pairs.append((l[x][1], l[y][1]))

	return pairs

def get_avg_time(obj, obj_lists):
	"""
	Gets the average time an object was requested out of all page loads.
	Used for ordering output.
	"""
	times = []
	for obj_list in obj_lists:
		for o in obj_list:
			if o[1] == obj:
				times.append(o[0])

	return numpy.mean(times)

def get_first_gap(obj_lists):
	"""
	We need a gap (time between object requests) that suggests a dependence between objects
	after and after the gap.  Currently we use half of the average times between the requests
	for the first and second objects.
	XXX: This seems to work OK but could be investigated further	
	"""
	gaps = []
	for l in obj_lists:
		gaps.append(l[1][2])	# time delta of second object loaded (i.e. first object after index.html)
	return .5*numpy.mean(gaps)

def add_tds(l):
	"""
	Given a list of (request time, object) tuples, calculates the time difference between
	each object.  Returns a list of (request time, object, time delta) tuples, where
	time delta is the amount of time between the object and the prior object's request times.
	"""
	prev_st = None
	for i, obj in enumerate(l):
		tmp_obj = list(obj)
		if prev_st != None:
			tmp_obj[2] = obj[0]-prev_st
		else:
			tmp_obj[2] = 0
		prev_st = obj[0]
		l[i] = tuple(tmp_obj)

	return l

def main(args):
	with open(args.har_file) as f:
		log = json.load(f)

	# Parse the HAR file to get a list of (request time, object) tuples.
	# obj_lists is a list of these lists, one per site load
	obj_lists = []
	cur_list = []
	pageref = log['log']['entries'][0]['pageref']
	for ent in log['log']['entries']:
		if ent['pageref'] != pageref:
			obj_lists.append(add_tds(sorted(cur_list)))
			cur_list = []
			pageref = ent['pageref']

		obj = ent['request']['url']
		st = parser.parse(ent['startedDateTime'])
		
		st_seconds = (st-datetime.datetime(1970,1,1,tzinfo=st.tzinfo)).total_seconds()
		cur_list.append((st_seconds, obj, 0))
	obj_lists.append(add_tds(sorted(cur_list)))

	gap = get_first_gap(obj_lists)
	print('Gap: {} seconds'.format(gap))

	# Iterates through each list looking for gaps that indicate a possible object dependency
	dep_pairs = {}
	parents = {}
	for j, l in enumerate(obj_lists):
		pairs = []
		for i, (st_seconds, obj, time_delta_seconds) in enumerate(l):
			if time_delta_seconds >= gap:
				# pairs is a list of possible dependencies in the form of (parent,child) tuples
				pairs += get_gap_objs(l, i, args.mergelist_file, gap)
		# dedup pairs by casting to a set, then count and collect kids
		# need to dedup because if there are multiple gaps close together we might get the same dep pair
		# for each gap, which would be double counting
		# dep_pairs is a dict that just counts each pair, key is the (parent,child) tuple value is count
		for p in set(pairs):
			if p not in dep_pairs:
				dep_pairs[p] = 0
			dep_pairs[p] += 1

	# Eliminate pairs that didn't appear in enough page loads
	for k in dep_pairs.keys():
		if dep_pairs[k] < 25:
			del dep_pairs[k]
	for l in obj_lists:
		objs = [obj for (_, obj, _) in l]
		sts = [st for (st, _, _) in l]
		# for each pair...
		for (parent, kid) in dep_pairs.keys():
			# if the kid is in this list but the parent isn't, delete it
			if kid in objs and parent not in objs:
				del dep_pairs[(parent, kid)]
			# or if they're both in the list but the kid was before the parent
			elif kid in objs and parent in objs and sts[objs.index(kid)] < sts[objs.index(parent)]:
				del dep_pairs[(parent, kid)]

			# Only push "important" file types (no images)
			# XXX look into this...
			kid = '{}://{}{}'.format(*urlparse.urlparse(kid)[:3])
			if os.path.splitext(kid)[1] not in ('.html', '.js', '.css', '.htm', '.php', '', '.ttf'):
				try:
					del dep_pairs[(parent, kid)]
				except KeyError as e:
					print e
					pass

	# Collect kids of parents into deps dict.  Keys are parent objects (strings), values are a list of
	# that parent's kids
	deps = {}
	for (p, k) in dep_pairs.keys():
		if p not in deps:
			deps[p] = []
		if k not in deps[p]:
			deps[p].append(k)

	# Get times each parent was requested, will be used to put them in the push config file in chronological order
	parent_times = {}
	for p in deps.keys():
		parent_times[p] = get_avg_time(p, obj_lists)

	write_output(deps, parent_times, args.output_file, args.output_template_file, args.hostname, args.hostname_dirs_file)

if __name__ == '__main__':
	aparser = argparse.ArgumentParser()
	aparser.add_argument('--har', dest='har_file', required=True)
	aparser.add_argument('--output', dest='output_file', required=True)
	aparser.add_argument('--mergelist', dest='mergelist_file', required=True)
	aparser.add_argument('--output-template', dest='output_template_file', required=True)
	aparser.add_argument('--hostname', dest='hostname', required=True)
	aparser.add_argument('--hostname-dirs', dest='hostname_dirs_file', required=True)
	args = aparser.parse_args()

	main(args)
