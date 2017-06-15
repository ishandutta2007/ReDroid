from multiprocessing import Process, Pool

import json
import os
import argparse
import subprocess
import re
import numpy
import scipy.optimize


TRACE_VERSION_RE = re.compile(r"VERSION: ([0-9]+)")
TRACE_NUM_RE = re.compile(r"Threads \(([0-9]+)\):")
TRACE_ITEM_RE = re.compile(r"([0-9]+)[ \t]+(ent|xit|unr)(!*)[ \t]+([0-9]+)[ \-\+]([^ \t]+)[ \t]+([^ \t]+)[ \t]+([^ \t]+)")


def trace_str_to_class_method(trace_str):
    trace_idx = len("ent ")
    while not trace_str[trace_idx].isalpha():
        trace_idx += 1
    return trace_str[trace_idx:]

def clean_trace(trace_list):
    # Clean irrelevant traces like java.lang, android.view
    ex_class_list = [
        "android.",
        "com.android",
        "com.google.android.collect",
        "dalvik.system",
        "java.",
        "libcore.",
        "sun.",
    ]

    ret_trace_list = []
    for trace_str in trace_list:
        trimmed_trace_str = trace_str_to_class_method(trace_str)
        trace_removed = False
        for ex_class in ex_class_list:
            if trimmed_trace_str.startswith(ex_class):
                trace_removed = True
                break
        if not trace_removed:
            ret_trace_list.append(trace_str)

    return ret_trace_list


def process_trace(trace_str):
    trace_lines = trace_str.split(os.linesep)
    trace_obj = {}

    idx = 0
    trace_obj["version"] = int(TRACE_VERSION_RE.match(trace_lines[idx]).groups()[0])
    idx += 1
    thread_num = int(TRACE_NUM_RE.match(trace_lines[idx]).groups()[0])
    idx += 1
    trace_obj["thread_info"] = {}
    for i in range(thread_num):
        thread_name_start_idx = trace_lines[i + idx].find(" ") + 1
        tid = int(trace_lines[i + idx][:thread_name_start_idx])
        trace_obj["thread_info"][tid] = {}
        trace_obj["thread_info"][tid]["name"] = trace_lines[i + idx][thread_name_start_idx:]
        trace_obj["thread_info"][tid]["trace"] = []
    idx += thread_num + 1

    while len(trace_lines[idx]):
        line_info = TRACE_ITEM_RE.match(trace_lines[idx]).groups()
        trace_obj["thread_info"][int(line_info[0])]["trace"].append(
            "%s%s %s %s %s" % (line_info[1], line_info[2], line_info[4], line_info[5], line_info[6])
        )
        idx += 1
    # get rid of empty traces
    tids = trace_obj["thread_info"].keys()
    for tid in tids:
        if not len(trace_obj["thread_info"][tid]["trace"]):
            trace_obj["thread_info"].pop(tid)
    return trace_obj


def trace_similarity(name_a, trace_a, name_b, trace_b):
    # cov similarity and name similarity

    class_methods_a = set()
    class_methods_b = set()
    for trace_str in trace_a:
        class_methods_a.add(trace_str_to_class_method(trace_str))
    for trace_str in trace_b:
        class_methods_b.add(trace_str_to_class_method(trace_str))

    name_sim = float(len(os.path.commonprefix([name_a, name_b]))) / max(len(name_a), len(name_b))
    cov_sim = float(len(class_methods_a & class_methods_b)) / (len(class_methods_a | class_methods_b))
    return name_sim * cov_sim


def compare_trace(real_device_trace_path, emulator_trace_path, output_file_path):
    # There might be various kinds of differences between real/emu threads
    # including
    # 1. different triggered threads
    # 2. unaligned traces
    # 3. different tracing time length
    # For now we only try to detect anti-sandbox behaviors in
    # ALIGNED, MATCHED and TRUNKED traces.
    # we ASSUME that every anti-sandbox behavior presented BESIDES the conditions
    # above are all triggered originally from the conditions above.

    # Maybe we can sort traces into different channels by its calling stack
    # depth, after the diverge point.

    # 1. use incremental coverage instead of tracing when comparing
    # 2. filter out some irrelevant methods (now using)
    # 3. automatically generate irrelevant methods by repeating dynamic tests

    p = subprocess.Popen(["dmtracedump", "-o", real_device_trace_path], stdout=subprocess.PIPE)
    real_device_trace_str = p.communicate()[0]
    p = subprocess.Popen(["dmtracedump", "-o", emulator_trace_path], stdout=subprocess.PIPE)
    emulator_trace_str = p.communicate()[0]

    real_device_trace_obj = process_trace(real_device_trace_str)
    emulator_trace_obj = process_trace(emulator_trace_str)

    # Kuhn-Munkres algorithm for maximum similarity
    r_tid_list = sorted(real_device_trace_obj["thread_info"].keys())
    e_tid_list = sorted(emulator_trace_obj["thread_info"].keys())
    sim_matrix = numpy.zeros([len(r_tid_list), len(e_tid_list)])
    for i, tid_r in enumerate(r_tid_list):
        for j, tid_e in enumerate(e_tid_list):
            sim_matrix[i][j] = -trace_similarity(
                real_device_trace_obj["thread_info"][tid_r]["name"],
                real_device_trace_obj["thread_info"][tid_r]["trace"],
                emulator_trace_obj["thread_info"][tid_e]["name"],
                emulator_trace_obj["thread_info"][tid_e]["trace"]
            )
    r_idx, e_idx = scipy.optimize.linear_sum_assignment(sim_matrix)
    trace_similarity_list = []
    for x, y in zip(r_idx, e_idx):
        real_device_trace = clean_trace(real_device_trace_obj["thread_info"][r_tid_list[x]]["trace"])
        emulator_trace = clean_trace(emulator_trace_obj["thread_info"][e_tid_list[y]]["trace"])

        trace_aligned = len(real_device_trace) == 0 or \
                        len(emulator_trace) == 0 or \
                        real_device_trace[0] == emulator_trace[0]
        if trace_aligned:
            trace_idx = 0
            max_common_len = min(len(real_device_trace), len(emulator_trace))
            while trace_idx < max_common_len:
                if real_device_trace[trace_idx] != emulator_trace[trace_idx]:
                    break
                else:
                    trace_idx += 1
            trace_similarity_list.append({
                "real_id": r_tid_list[x],
                "real_name": real_device_trace_obj["thread_info"][r_tid_list[x]]["name"],
                "real_trace": real_device_trace[max(0, trace_idx - 1):trace_idx + 1] if trace_idx < max_common_len else None,
                "emu_id": e_tid_list[y],
                "emu_name": emulator_trace_obj["thread_info"][e_tid_list[y]]["name"],
                "e_trace": emulator_trace[max(0, trace_idx - 1):trace_idx + 1] if trace_idx < max_common_len else None,
                "sim_cov": -sim_matrix[x][y],
                "max_common_len": max_common_len,
                "diverge_idx": trace_idx,
                "sim_max_common": float(trace_idx) / max_common_len if max_common_len else 1.0
            })

    with open(output_file_path, "w") as output_file:
        output_file.write(json.dumps(trace_similarity_list, indent=2))

    return "%s written" % output_file_path

def run(config_json_path):
    """
    parse config file
    assign work to multiple vm/device's
    """
    config_json = json.load(open(os.path.abspath(config_json_path), "r"))

    real_device_droidbot_out_dir = os.path.abspath(config_json["real_device_droidbot_out_dir"])
    emulator_droidbot_out_dir = os.path.abspath(config_json["emulator_droidbot_out_dir"])
    output_dir = os.path.abspath(config_json["output_dir"])
    if os.system("mkdir -p %s" % output_dir):
        print "failed mkdir -p %s" % output_dir
        return
    process_num = config_json["process_num"]

    real_device_apps = [x for x in os.walk(real_device_droidbot_out_dir).next()[1]]
    emulator_apps = [x for x in os.walk(emulator_droidbot_out_dir).next()[1]]
    both_apps = list(set(real_device_apps) & set(emulator_apps))

    # generate trace path pairs for comparing
    pool = Pool(processes=process_num)
    result_list = []
    for app_name in both_apps:
        real_device_path = "%s/%s/events" % (real_device_droidbot_out_dir, app_name)
        emulator_path = "%s/%s/events" % (emulator_droidbot_out_dir, app_name)

        real_device_traces = sorted([x for x in os.walk(real_device_path).next()[2]
                                     if x.endswith(".trace")])
        emulator_traces = sorted([x for x in os.walk(emulator_path).next()[2]
                                  if x.endswith(".trace")])

        for x, y in zip(real_device_traces, emulator_traces):
            x_tag = x[len("event_trace_"):-len(".trace")]
            y_tag = y[len("event_trace_"):-len(".trace")]
            async_result = pool.apply_async(compare_trace,
                                            ["%s/%s" % (real_device_path, x),
                                             "%s/%s" % (emulator_path, y),
                                             "%s/%s_%s_%s.json" % (output_dir, app_name, x_tag, y_tag)])
            result_list.append(async_result)

    for async_result in result_list:
        print async_result.get()

    pool.close()
    pool.join()


def parse_args():
    """
    parse command line input
    """
    parser = argparse.ArgumentParser(description="Compare traces collected from real devices and emulators")
    parser.add_argument("-c", action="store", dest="config_json_path",
                        required=True, help="path/to/trace_comparator_config.json")
    options = parser.parse_args()
    return options


def main():
    """
    the main function
    """
    opts = parse_args()
    run(opts.config_json_path)
    return


if __name__ == "__main__":
    main()
