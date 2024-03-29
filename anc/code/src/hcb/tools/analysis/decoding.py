import pathlib
import sys
from typing import Callable, List, Optional
import math
import subprocess
import tempfile

import networkx as nx
import numpy as np
import pymatching
import stim


def sample_decode_count_correct(*,
                                circuit: stim.Circuit,
                                model_circuit: Optional[stim.Circuit] = None,
                                num_shots: int,
                                decoder: str) -> int:
    """Counts how many times a decoder correctly predicts the logical frame of simulated runs.

    Args:
        circuit: The circuit to sample from and decode results for.
        model_circuit: The circuit to use to generate the error model. Defaults to be the same thing as
            the circuit being sampled from.
        num_shots: The number of sample shots to take from the cirucit.
        decoder: The name of the decoder to use. Allowed values are:
            "pymatching": Use pymatching.
            "internal": Use an internal decoder at `src/internal_decoder.binary` (not publically available).
            "internal_correlated": Use the internal decoder and tell it to do correlated decoding.
    """
    if decoder == "pymatching":
        use_internal_decoder = False
        use_correlated_decoding = False
    elif decoder == "internal":
        use_internal_decoder = True
        use_correlated_decoding = False
    elif decoder == "internal_correlated":
        use_internal_decoder = True
        use_correlated_decoding = True
    else:
        raise NotImplementedError(f"{decoder=!r}")

    num_dets = circuit.num_detectors
    num_obs = circuit.num_observables
    if model_circuit is None:
        model_circuit = circuit
    else:
        assert model_circuit.num_detectors == num_dets
        assert model_circuit.num_observables == num_obs

    # Sample some runs with known solutions.
    bit_packed_det_obs_samples = circuit.compile_detector_sampler().sample_bit_packed(num_shots, append_observables=True)
    if num_obs == 0:
        bit_packed_det_samples = bit_packed_det_obs_samples
        obs_samples = np.zeros(shape=(bit_packed_det_samples.shape[0], 0), dtype=np.bool8)
    else:
        num_det_bytes = (num_dets + 7) // 8
        num_obs_bytes = (num_dets % 8 + num_obs + 7) // 8
        obs_samples = bit_packed_det_obs_samples[:, -num_obs_bytes:]
        obs_samples = np.unpackbits(obs_samples, axis=1, count=num_obs_bytes * 8, bitorder='little')
        obs_samples = obs_samples[:, num_dets % 8:][:, :num_obs]
        bit_packed_det_samples = bit_packed_det_obs_samples[:, :num_det_bytes]
        rem = num_dets % 8
        if rem:
            bit_packed_det_samples[:, -1] &= np.uint8((1 << rem) - 1)
    assert obs_samples.shape[0] == bit_packed_det_samples.shape[0]
    assert obs_samples.shape[1] == num_obs
    assert bit_packed_det_samples.shape[1] == (num_dets + 7) // 8

    # Have the decoder produce the solution from the symptoms.
    decode_method = decode_using_internal_decoder if use_internal_decoder else decode_using_pymatching
    predictions = decode_method(
        bit_packed_det_samples=bit_packed_det_samples,
        circuit=model_circuit,
        use_correlated_decoding=use_correlated_decoding,
    )

    # Count how many solutions were completely correct.
    assert predictions.shape == obs_samples.shape
    all_corrects = np.all(predictions == obs_samples, axis=1)
    return np.count_nonzero(all_corrects)


def decode_using_pymatching(circuit: stim.Circuit,
                            bit_packed_det_samples: np.ndarray,
                            use_correlated_decoding: bool,
                            ) -> np.ndarray:
    """Collect statistics on how often logical errors occur when correcting using detections."""
    if use_correlated_decoding:
        raise NotImplementedError("pymatching doesn't support correlated decoding")

    error_model = circuit.detector_error_model(decompose_errors=True)
    matching_graph = detector_error_model_to_pymatching_graph(error_model)

    num_shots = bit_packed_det_samples.shape[0]
    num_obs = circuit.num_observables
    num_dets = circuit.num_detectors
    assert bit_packed_det_samples.shape[1] == (num_dets + 7) // 8

    predictions = np.zeros(shape=(num_shots, num_obs), dtype=np.bool8)
    for k in range(num_shots):
        expanded_det = np.unpackbits(bit_packed_det_samples[k], count=num_dets, bitorder='little')
        expanded_det = np.resize(expanded_det, num_dets + 1)
        expanded_det[-1] = 0
        predictions[k] = matching_graph.decode(expanded_det)
    return predictions


def internal_decoder_path() -> Optional[str]:
    src_directory = pathlib.Path(__file__).parent.parent.parent.parent.parent.resolve()
    decoder_path = src_directory / "lib" / "internal_decoder.binary"
    if decoder_path.exists():
        return decoder_path
    return None


def repro_output_path() -> pathlib.Path:
    return pathlib.Path(__file__).parent.parent.parent.parent.parent.resolve() / 'out'


def decode_using_internal_decoder(circuit: stim.Circuit,
                                  bit_packed_det_samples: np.ndarray,
                                  use_correlated_decoding: bool,
                                  ) -> np.ndarray:
    num_shots = bit_packed_det_samples.shape[0]
    num_obs = circuit.num_observables
    assert bit_packed_det_samples.shape[1] == (circuit.num_detectors + 7) // 8
    error_model = circuit.detector_error_model(decompose_errors=True)

    with tempfile.TemporaryDirectory() as d:
        dem_file = f"{d}/model.dem"
        dets_file = f"{d}/dets.b8"
        out_file = f"{d}/predictions.01"

        with open(dem_file, "w") as f:
            print(error_model, file=f)
        with open(dets_file, "wb") as f:
            bit_packed_det_samples.tofile(f)

        path = internal_decoder_path()
        if path is None:
            raise RuntimeError(
                "You need an `internal_decoder.binary` file in the working directory to "
                "use `decoder=internal` or `decoder=internal_correlated`.")

        command = (f"{path} "
                   f"-mode fi_match_from_dem "
                   f"-dets_format b8 "
                   f"-dem_fname '{dem_file}' "
                   f"-dets_fname '{dets_file}' "
                   f"-ignore_distance_1_errors "
                   f"-out '{out_file}'")
        if use_correlated_decoding:
            command += " -cheap_corr -edge_corr -node_corr"
        try:
            subprocess.check_output(command, shell=True)
        except:
            with open(dem_file) as f:
                with open(repro_output_path() / "repro_model.dem", "w") as f2:
                    print(f.read(), file=f2)
            with open(dets_file, 'rb') as f:
                with open(repro_output_path() / "repro_dets.b8", "wb") as f2:
                    f2.write(f.read())
            with open(repro_output_path() / "repro_circuit.stim", "w") as f2:
                print(circuit, file=f2)
            print(f"Wrote case to `repro.dem`, `repro.dets`, and `repro.stim`.\n"
                  f"Command line was: {command}", file=sys.stderr)
            raise

        predictions = np.zeros(shape=(num_shots, num_obs), dtype=np.bool8)
        with open(out_file, "r") as f:
            for shot in range(num_shots):
                for obs_index in range(num_obs):
                    c = f.read(1)
                    assert c in '01'
                    predictions[shot, obs_index] = c == '1'
                assert f.read(1) == '\n'

        return predictions


def iter_flatten_model(model: stim.DetectorErrorModel,
                       handle_error: Callable[[float, List[int], List[int]], None],
                       handle_detector_coords: Callable[[int, np.ndarray], None]):
    det_offset = 0
    coords_offset = np.zeros(100, dtype=np.float64)

    def _helper(m: stim.DetectorErrorModel, reps: int):
        nonlocal det_offset
        nonlocal coords_offset
        for _ in range(reps):
            for instruction in m:
                if isinstance(instruction, stim.DemRepeatBlock):
                    _helper(instruction.body_copy(), instruction.repeat_count)
                elif isinstance(instruction, stim.DemInstruction):
                    if instruction.type == "error":
                        dets: List[int] = []
                        frames: List[int] = []
                        t: stim.DemTarget
                        p = instruction.args_copy()[0]
                        for t in instruction.targets_copy():
                            if t.is_relative_detector_id():
                                dets.append(t.val + det_offset)
                            elif t.is_logical_observable_id():
                                frames.append(t.val)
                            elif t.is_separator():
                                # Treat each component of a decomposed error as an independent error.
                                # (Ideally we could configure some sort of correlated analysis; oh well.)
                                handle_error(p, dets, frames)
                                frames = []
                                dets = []
                        # Handle last component.
                        handle_error(p, dets, frames)
                    elif instruction.type == "shift_detectors":
                        det_offset += instruction.targets_copy()[0]
                        a = np.array(instruction.args_copy())
                        coords_offset[:len(a)] += a
                    elif instruction.type == "detector":
                        a = np.array(instruction.args_copy())
                        for t in instruction.targets_copy():
                            handle_detector_coords(t.val + det_offset, a + coords_offset[:len(a)])
                    elif instruction.type == "logical_observable":
                        pass
                    else:
                        raise NotImplementedError()
                else:
                    raise NotImplementedError()
    _helper(model, 1)


def detector_error_model_to_nx_graph(model: stim.DetectorErrorModel) -> nx.Graph:
    """Convert a stim error model into a NetworkX graph."""

    g = nx.Graph()
    boundary_node = model.num_detectors
    g.add_node(boundary_node, is_boundary=True, coords=[-1, -1, -1])

    def handle_error(p: float, dets: List[int], frame_changes: List[int]):
        if p == 0:
            return
        if len(dets) == 0:
            # No symptoms for this error.
            # Code probably has distance 1.
            # Accept it and keep going, though of course decoding will probably perform terribly.
            return
        if len(dets) == 1:
            dets = [dets[0], boundary_node]
        if len(dets) > 2:
            raise NotImplementedError(
                f"Error with more than 2 symptoms can't become an edge or boundary edge: {dets!r}.")
        if g.has_edge(*dets):
            edge_data = g.get_edge_data(*dets)
            old_p = edge_data["error_probability"]
            old_frame_changes = edge_data["qubit_id"]
            # If frame changes differ, the code has distance 2; just keep whichever was first.
            if set(old_frame_changes) == set(frame_changes):
                p = p * (1 - old_p) + old_p * (1 - p)
                g.remove_edge(*dets)
        g.add_edge(*dets, weight=math.log((1 - p) / p), qubit_id=frame_changes, error_probability=p)

    def handle_detector_coords(detector: int, coords: np.ndarray):
        g.add_node(detector, coords=coords)

    iter_flatten_model(model, handle_error=handle_error, handle_detector_coords=handle_detector_coords)

    return g


def detector_error_model_to_pymatching_graph(model: stim.DetectorErrorModel) -> pymatching.Matching:
    """Convert a stim error model into a pymatching graph."""
    g = detector_error_model_to_nx_graph(model)
    num_detectors = model.num_detectors
    num_observables = model.num_observables

    # Add spandrels to the graph to ensure pymatching will accept it.
    # - Make sure there's only one connected component.
    # - Make sure no detector nodes are skipped.
    # - Make sure no observable nodes are skipped.
    for k in range(num_detectors):
        g.add_node(k)
    g.add_node(num_detectors + 1)
    for k in range(num_detectors + 1):
        g.add_edge(k, num_detectors + 1, weight=9999999999)
    g.add_edge(num_detectors, num_detectors + 1, weight=9999999999, qubit_id=list(range(num_observables)))

    return pymatching.Matching(g)
