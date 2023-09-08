import json
from typing import Dict, Optional, Tuple

from aiida.orm import CalcJobNode, QueryBuilder, RemoteData, SinglefileData

from aiida_aurora.data import BatterySampleData
from aiida_aurora.utils.parsers import get_data_from_raw, get_data_from_results


def cycling_analysis(node: CalcJobNode) -> Tuple[dict, str]:
    """Perform post-processing of cycling experiments results.

    Used by the frontend Aurora app for plotting.

    The analysis report attached to each plot series includes:
    - details of the associated monitor, if one was assigned
    - summary/statistics of the post-processed data

    Parameters
    ----------
    `node` : `CalcJobNode`
        The calculation `node`.

    Returns
    -------
    `Tuple[dict, str]`
        Post-processed data and an analysis report.

    Raises
    ------
    `TypeError`
        If `node` is not a `BatteryCyclerExperiment`.
    """

    if node.process_type != "aiida.calculations:aurora.cycler":
        raise TypeError("`node` is not a `BatteryCyclerExperiment`")

    report = f"CalcJob:   <{node.pk}> '{node.label}'\n"

    sample: BatterySampleData = node.inputs.battery_sample
    report += f"Sample:    {sample.label}\n"

    report += "Monitored: "

    if monitors := get_monitors(node):
        report += "True\n"
        report += add_monitor_details(monitors)
    else:
        report += "False\n"

    try:
        data, analysis = process_data(node)
    except Exception as err:
        data, analysis = {}, f"*** ERROR ***\n\n{str(err)}"

    return (data, f"{report}\n{analysis}")


def get_monitors(node: CalcJobNode) -> Dict[str, dict]:
    """Fetch the monitor dictionary.

    The function is backwards compatible, capable of fetching the
    dictionary of monitors defined in the AiiDA 2.x monitoring
    feature, or, if the calculation was submitted prior to the
    AiiDA 2.x update, fetch the associated monitor calcjob and
    prepare the dictionary in accordance with the new format.

    Parameters
    ----------
    `node` : `CalcJobNode`
        The calculation node.

    Returns
    -------
    `Dict[str, dict]`
        A dictionary of monitors.
    """

    if "monitors" in node.inputs:
        return {k: dict(v) for k, v in dict(node.inputs.monitors).items()}

    # BACKWARDS COMPATABILITY
    # job submitted prior to AiiDA 2.x upgrade - fetch monitor calcjob
    monitor = get_node_monitor_calcjob(node)
    return convert_to_new_monitor_format(monitor) if monitor else {}


def get_node_monitor_calcjob(node: CalcJobNode) -> Optional[CalcJobNode]:
    """Fetch the monitor calcjob associated with the calculation
    `node`.

    Uses the `QueryBuilder` to query for a monitor calcjob with
    a `RemoteData` node associated with the calculation node.

    Parameters
    ----------
    `node` : `CalcJobNode`
        The calculation node.

    Returns
    -------
    `Optional[CalcJobNode]`
        The associated monitor calcjob node, `None` if not found.
    """

    if "remote_folder" not in node.outputs:
        return None

    remote_folder: RemoteData = node.outputs.remote_folder

    qb = QueryBuilder()

    qb.append(
        RemoteData,
        filters={
            'uuid': remote_folder.uuid,
        },
        tag='remote_folder',
    ).append(
        CalcJobNode,
        with_incoming='remote_folder',
        edge_filters={
            'label': 'monitor_folder'
        },
        project=['*', 'id'],
        tag='monitor',
    ).order_by({
        'monitor': {
            'id': 'desc'
        },
    })

    results = qb.first() if qb.count() else None
    return results[0] if results else None


def convert_to_new_monitor_format(monitor: CalcJobNode) -> Dict[str, dict]:
    """Convert monitor calcjob attributes to AiiDA 2.x format.

    For more details, see

    https://aiida.readthedocs.io/projects/aiida-core/en/latest/howto/run_codes.html#how-to-monitor-and-prematurely-stop-a-calculation

    Parameters
    ----------
    `monitor` : `CalcJobNode`
        The monitor calcjob `node`.

    Returns
    -------
    `Dict[str, dict]`
        The formatted monitor dictionary.
    """

    protocols: dict = monitor.inputs.monitor_protocols
    params: dict = protocols["monitor1"].attributes

    settings: dict = params.get("options", {})
    sources: dict = params.get("sources", {})
    extra: dict = sources.get("output", {})

    refresh_rate = extra.get("refresh_rate", 600)
    filename = extra.get("filepath", "snapshot.json")

    threshold = settings.get("threshold", 0.8)
    check_type = settings.get("check_type", "discharge_capacity")
    consecutive_cycles = settings.get("consecutive_cycles", 2)

    return {
        "capacity": {
            "entry_point": f"aiida-calcmonitor plugin <pk={monitor.pk}>",
            "minimum_poll_interval": refresh_rate,
            "kwargs": {
                "filename": filename,
                "settings": {
                    "threshold": threshold,
                    "check_type": check_type,
                    "consecutive_cycles": consecutive_cycles,
                }
            }
        }
    }


def add_monitor_details(monitors: Dict[str, dict]) -> str:
    """Return monitor details.

    Details include the following:
    - monitor label
    - AiiDA entry point of the monitor function/calcjob
    - refresh (polling) rate
    - source file to be polled
    - monitor settings

    Parameters
    ----------
    `monitors` : `Dict[str, dict]`
        A dictionary of monitors.

    Returns
    -------
    `str`
        The monitor details to be added to the analysis report.
    """

    details = "" if monitors else "\nWARNING: No monitors found\n"

    for label, params in monitors.items():
        details += f"\nMonitor:              {label}\n"
        entry_point = params.get("entry_point", "aiida-calcmonitor plugin")
        refresh_rate = params.get("minimum_poll_interval", 600)
        details += f"  Entry point:        {entry_point}\n"
        details += f"  Interval (s):       {refresh_rate}\n"

        if "kwargs" in params:
            kwargs: dict = params["kwargs"]
            source_file = kwargs.get("filename", "snapshot.json")
            settings: dict = kwargs.get("settings", {})
            details += add_monitor_settings(source_file, settings)

    return details


def add_monitor_settings(
    source_file: str,
    settings: dict,
) -> str:
    """Return specific monitor settings details.

    NOTE: Setting keys are sentence-cased.

    Parameters
    ----------
    `source_file` : `str`
        The polled source file.
    `settings` : `dict`
        The monitor settings.

    Returns
    -------
    `str`
        Details of the monitor settings.
    """

    _settings = f"  Source file:        {source_file}\n"

    check_type = settings.pop("check_type", "discharge_capacity")
    _settings += f"  Check type:         {check_type}\n"

    key: str
    for key, value in settings.items():
        key = key.replace("_", " ").capitalize() + ":"
        _settings += f"  {key:19s} {value}\n"

    return _settings


def process_data(node: CalcJobNode) -> Tuple[dict, str]:
    """Analyze the results of the cycling experiment.

    The analysis is performed on the results `ArrayNode`, if one
    was prepared by AiiDA upon a successful run. If not, in the
    case the job was terminated prematurely, the function will
    attempt to analyze (in order) the raw (non-parsed) results,
    the retrieved results file, or if none was retrieved, the
    snapshot fetched directly from the remote machine.

    Parameters
    ----------
    `node` : `CalcJobNode`
        The calculation `node`.

    Returns
    -------
    `Tuple[dict, str]`
        Post-processed data and an analysis report.
    """

    if node.process_state and "finished" not in node.process_state.value:
        return {}, f"Job terminated with message '{node.process_status}'"

    report = ""

    if node.exit_status:
        report += "WARNING: "
        generic = "job killed by monitor"
        report += f"{node.exit_message}" if node.exit_message else generic
        report += "\n\n"

    if "results" in node.outputs:
        data = get_data_from_results(node.outputs.results)
    elif "raw_data" in node.outputs:
        data = get_data_from_file(node.outputs.raw_data)
    elif "retrieved" in node.outputs:
        data = get_data_from_file(node.outputs.retrieved)
    elif "remote_folder" in node.outputs:
        data = get_data_from_remote(node.outputs.remote_folder)
    else:
        data = {}

    # TODO extract data summary/statistics
    report += add_analysis(data)

    return data, report


def get_data_from_file(source: SinglefileData) -> dict:
    """Return source file as a post-processed dictionary.

    NOTE: assumes file is of JSON format.

    Parameters
    ----------
    `source` : `SinglefileData`
        The node of the associated retrieved results file.

    Returns
    -------
    `dict`
        The post-processed data dictionary.
    """
    if "results.json" in source.base.repository.list_object_names():
        file = source.base.repository.get_object_content("results.json")
        raw = json.loads(file)
        return get_data_from_raw(raw)
    return {}


def get_data_from_remote(source: RemoteData) -> dict:
    """Return fetched snapshot as a post-processed dictionary.

    Parameters
    ----------
    `source` : `RemoteData`
        The node of the remote folder containing the snapshot.

    Returns
    -------
    `dict`
        The post-processed data dictionary.
    """
    try:
        remote_path = source.get_attribute("remote_path")
        with open(f"{remote_path}/snapshot.json") as file:
            return get_data_from_file(file)
    except Exception:
        return {}


def add_analysis(data: dict) -> str:
    """Return analysis details.

    Parameters
    ----------
    `data` : `dict`
        The post-processed data dictionary.

    Returns
    -------
    `str`
        The details of the analysis.
    """
    # TODO replace str(data) with something insightful, and clean!
    return str(data) if data else "ERROR! Failed to find or parse output"
