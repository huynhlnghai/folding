import os
import time
import glob
import base64
import concurrent.futures
from typing import Dict, List, Tuple
from collections import defaultdict
import bittensor as bt

# import base miner class which takes care of most of the boilerplate
from folding.base.miner import BaseMinerNeuron
from folding.protocol import JobSubmissionSynapse
from folding.utils.logging import log_event
from folding.utils.ops import (
    run_cmd_commands,
    check_if_directory_exists,
    get_tracebacks,
    calc_potential_from_edr,
)

# root level directory for the project (I HATE THIS)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_DATA_PATH = os.path.join(ROOT_DIR, "miner-data")


def attach_files(
    files_to_attach: List, synapse: JobSubmissionSynapse
) -> JobSubmissionSynapse:
    """function that parses a list of files and attaches them to the synapse object"""
    bt.logging.info(f"Sending files to validator: {files_to_attach}")
    for filename in files_to_attach:
        # trrs are large, and validators don't need them.
        if filename.endswith(".trr"):
            continue

        try:
            with open(filename, "rb") as f:
                filename = filename.split("/")[
                    -1
                ]  # remove the directory from the filename
                synapse.md_output[filename] = base64.b64encode(f.read())
        except Exception as e:
            bt.logging.error(f"Failed to read file {filename!r} with error: {e}")
            get_tracebacks()

    return synapse


def attach_files_to_synapse(
    synapse: JobSubmissionSynapse,
    data_directory: str,
    state: str,
) -> JobSubmissionSynapse:
    """load the output files as bytes and add to synapse.md_output

    Args:
        synapse (JobSubmissionSynapse): Recently received synapse object
        data_directory (str): directory where the miner is holding the necessary data for the validator.
        state (str): the current state of the simulation

    state is either:
     1. nvt
     2. npt
     3. md_0_1
     4. finished

    State depends on the current state of the simulation (controlled in GromacsExecutor.run() method).

    During the simulation procedure, the validator queries the miner for the current state of the simulation.
    The files that the miner needs to return are:
        1. .tpr (created during grompp commands)
        2. .xtc (created during mdrun commands, logged every nstxout-compressed steps)
        3. .cpt (created during mdrun commands, logged every nstcheckpoint steps) # TODO: remove (re create .gro file from .tpr and .xtc)


    Returns:
        JobSubmissionSynapse: synapse with md_output attached
    """

    synapse.md_output = {}  # ensure that the initial state is empty

    try:
        state_files = os.path.join(
            data_directory, f"{state}"
        )  # mdrun commands make the filenames [state.*]

        # applying glob to state_files will get the necessary files we need (e.g. nvt.tpr, nvt.xtc, nvt.cpt, nvt.edr, etc.)
        all_state_files = glob.glob(f"{state_files}*")  # Grab all the state_files
        latest_cpt_file = glob.glob("*.cpt")

        files_to_attach: List = (
            all_state_files + latest_cpt_file
        )  # combine the state files and the latest checkpoint file

        if len(files_to_attach) == 0:
            raise FileNotFoundError(
                f"No files found for {state}"
            )  # if this happens, goes to except block

        synapse = attach_files(files_to_attach=files_to_attach, synapse=synapse)

    except Exception as e:
        bt.logging.error(
            f"Failed to attach files for pdb {synapse.pdb_id} with error: {e}"
        )
        get_tracebacks()
        synapse.md_output = {}
        # TODO Maybe in this point in the logic it makes sense to try and restart the sim.

    finally:
        return synapse  # either return the synapse wth the md_output attached or the synapse as is.


def check_synapse(
    self, synapse: JobSubmissionSynapse, output_dir: str, event: Dict = None
) -> JobSubmissionSynapse:
    """Utility function to remove md_inputs if they exist"""
    if len(synapse.md_inputs) > 0:
        event["md_inputs_sizes"] = list(map(len, synapse.md_inputs.values()))
        event["md_inputs_filenames"] = list(synapse.md_inputs.keys())
        synapse.md_inputs = {}  # remove from synapse

    if synapse.md_output is not None:
        event["md_output_sizes"] = list(map(len, synapse.md_output.values()))
        event["md_output_filenames"] = list(synapse.md_output.keys())

    if not self.config.wandb.off:
        energy_event = self.get_state_energies(output_dir=output_dir)
        event.update(energy_event)

    event["query_forward_time"] = time.time() - self.query_start_time

    log_event(self=self, event=event)
    return synapse


class FoldingMiner(BaseMinerNeuron):
    def __init__(self, config=None, base_data_path: str = None):
        super().__init__(config=config)

        # TODO: There needs to be a timeout manager. Right now, if
        # the simulation times out, the only time the memory is freed is when the miner
        # is restarted, or sampled again.

        self.base_data_path = (
            base_data_path
            if base_data_path is not None
            else os.path.join(BASE_DATA_PATH, self.wallet.hotkey.ss58_address[:8])
        )
        self.simulations = self.create_default_dict()

        self.max_workers = self.config.neuron.max_workers
        bt.logging.info(
            f"🚀 Starting FoldingMiner that handles {self.max_workers} workers 🚀"
        )

        self.executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=self.max_workers
        )  # remove one for safety

        self.mock = None

    def create_default_dict(self):
        def nested_dict():
            return defaultdict(
                lambda: None
            )  # allows us to set the desired attribute to anything.

        return defaultdict(nested_dict)

    def get_state_energies(self, output_dir: str) -> Dict:
        all_edr_files = glob.glob(os.path.join(output_dir, "*.edr"))
        state_potentials = []
        edr_files = []
        event = {}

        for file in all_edr_files:
            edr_name = file.split("/")[-1]
            try:
                state_potentials.append(
                    calc_potential_from_edr(output_dir=output_dir, edr_name=edr_name)
                )
                stats = os.stat(file)
                edr_files.append(
                    {
                        "name": edr_name,
                        "created_at": stats.st_ctime,
                        "modified_at": stats.st_ctime,
                        "size_bytes": stats.st_size,
                    }
                )
            except Exception as e:
                bt.logging.error(
                    f"Failed to calculate potential from edr file with error: {e}"
                )

        event["edr_files"] = edr_files
        event["state_energies"] = state_potentials
        return event

    def configure_commands(self, mdrun_args: str) -> Dict[str, List[str]]:
        gpu_args = "-nb gpu -pme gpu -bonded gpu -update gpu"  # GPU-specific arguments
        commands = [
            "gmx grompp -f nvt.mdp -c em.gro -r em.gro -p topol.top -o nvt.tpr",  # Temperature equilibration
            "gmx mdrun -deffnm nvt " + mdrun_args + " " + gpu_args,
            "gmx grompp -f npt.mdp -c nvt.gro -r nvt.gro -t nvt.cpt -p topol.top -o npt.tpr",  # Pressure equilibration
            "gmx mdrun -deffnm npt " + mdrun_args + " " + gpu_args,
            f"gmx grompp -f md.mdp -c npt.gro -t npt.cpt -p topol.top -o md_0_1.tpr",  # Production run
            f"gmx mdrun -deffnm md_0_1 " + mdrun_args + " " + gpu_args,
            f"echo '1\n1\n' | gmx trjconv -s md_0_1.tpr -f md_0_1.xtc -o md_0_1_center.xtc -center -pbc mol",
        ]

        # These are rough identifiers for the different states of the simulation
        state_commands = {
            "nvt": commands[:2],
            "npt": commands[2:4],
            "md_0_1": commands[4:],
        }

        return state_commands

    def check_and_remove_simulations(self, event: Dict) -> Dict:
        """Check to see if any simulations have finished, and remove them
        from the simulation store
        """
        if len(self.simulations) > 0:
            sims_to_delete = []

            for pdb_id, simulation in self.simulations.items():
                current_executor_state = simulation["executor"].get_state()

                if current_executor_state == "finished":
                    bt.logging.warning(
                        f"✅ {pdb_id} finished simulation... Removing from execution stack ✅"
                    )
                    sims_to_delete.append(pdb_id)

            for pdb_id in sims_to_delete:
                del self.simulations[pdb_id]

            event["running_simulations"] = list(self.simulations.keys())
            bt.logging.warning(f"Simulations Running: {list(self.simulations.keys())}")

        return event

    def forward(self, synapse: JobSubmissionSynapse) -> JobSubmissionSynapse:
        """
        The main async function that is called by the dendrite to run the simulation.
        There are a set of default behaviours the miner should carry out based on the form the synapse comes in as:

            1. Check to see if the pdb is in the set of simulations that are running
            2. If the synapse md_inputs contains a ckpt file, then we are expected to either accept/reject a simulation rebase. (not implemented yet)
            3. Check if simulation has been run before, and if so, return the files from the last simulation
            4. If none of the above conditions are met, we start a new simulation.
                - If the number of active processes is less than the number of CPUs and the pdb_id is unique, start a new process

        Returns:
            JobSubmissionSynapse: synapse with md_output attached
        """
        # If we are already running a process with the same identifier, return intermediate information
        bt.logging.debug(f"⌛ Query from validator for protein: {synapse.pdb_id} ⌛")

        # increment step counter everytime miner receives a query.
        self.step += 1
        self.query_start_time = time.time()

        event = self.create_default_dict()
        event["pdb_id"] = synapse.pdb_id

        output_dir = os.path.join(self.base_data_path, synapse.pdb_id)

        # check if any of the simulations have finished
        event = self.check_and_remove_simulations(event=event)

        # The set of RUNNING simulations.
        if synapse.pdb_id in self.simulations:
            self.simulations[synapse.pdb_id]["queried_at"] = time.time()
            simulation = self.simulations[synapse.pdb_id]
            current_executor_state = simulation["executor"].get_state()

            synapse = attach_files_to_synapse(
                synapse=synapse,
                data_directory=simulation["output_dir"],
                state=current_executor_state,
            )

            event["condition"] = "running_simulation"
            event["state"] = current_executor_state
            event["queried_at"] = simulation["queried_at"]

            return check_synapse(
                self=self, synapse=synapse, event=event, output_dir=output_dir
            )

        else:
            if os.path.exists(self.base_data_path) and synapse.pdb_id in os.listdir(
                self.base_data_path
            ):
                # If we have a pdb_id in the data directory, we can assume that the simulation has been run before
                # and we can return the COMPLETED files from the last simulation. This only works if you have kept the data.

                # We will attempt to read the state of the simulation from the state file
                state_file = os.path.join(output_dir, f"{synapse.pdb_id}_state.txt")

                # Open the state file that should be generated during the simulation.
                try:
                    with open(state_file, "r") as f:
                        lines = f.readlines()
                        state = lines[-1].strip()
                        state = "md_0_1" if state == "finished" else state

                    bt.logging.warning(
                        f"❗ Found existing data for protein: {synapse.pdb_id}... Sending previously computed, most advanced simulation state ❗"
                    )
                    synapse = attach_files_to_synapse(
                        synapse=synapse, data_directory=output_dir, state=state
                    )
                except Exception as e:
                    bt.logging.error(
                        f"Failed to read state file for protein {synapse.pdb_id} with error: {e}"
                    )
                    state = None

                event["condition"] = "found_existing_data"
                event["state"] = state

                return check_synapse(
                    self=self, synapse=synapse, event=event, output_dir=output_dir
                )

            elif len(self.simulations) >= self.max_workers:
                bt.logging.warning(
                    f"❗ Cannot start new process: job limit reached. ({len(self.simulations)}/{self.max_workers}).❗"
                )

                bt.logging.warning(f"❗ Removing miner from job pool ❗")

                event["condition"] = "cpu_limit_reached"
                synapse.miner_serving = False

                return check_synapse(
                    self=self, synapse=synapse, event=event, output_dir=output_dir
                )

            elif len(synapse.md_inputs) == 0:  # The vali sends nothing to the miner
                return check_synapse(
                    self=self, synapse=synapse, event=event, output_dir=output_dir
                )

        # TODO: also check if the md_inputs is empty here. If so, then the validator is broken
        state_commands = self.configure_commands(mdrun_args=synapse.mdrun_args)

        # Create the job and submit it to the executor
        simulation_manager = SimulationManager(
            pdb_id=synapse.pdb_id,
            output_dir=output_dir,
        )

        future = self.executor.submit(
            simulation_manager.run,
            synapse.md_inputs,
            state_commands,
            self.config.neuron.suppress_cmd_output,
            self.config.mock or self.mock,  # self.mock is inside of MockFoldingMiner
        )

        self.simulations[synapse.pdb_id]["executor"] = simulation_manager
        self.simulations[synapse.pdb_id]["future"] = future
        self.simulations[synapse.pdb_id]["output_dir"] = simulation_manager.output_dir
        self.simulations[synapse.pdb_id]["queried_at"] = time.time()

        bt.logging.success(
            f"✅ New pdb_id {synapse.pdb_id} submitted to job executor ✅ "
        )

        event["condition"] = "new_simulation"
        event["start_time"] = time.time()
        return check_synapse(
            self=self, synapse=synapse, event=event, output_dir=output_dir
        )

    async def blacklist(self, synapse: JobSubmissionSynapse) -> Tuple[bool, str]:
        if (
            not self.config.blacklist.allow_non_registered
            and synapse.dendrite.hotkey not in self.metagraph.hotkeys
        ):
            # Ignore requests from un-registered entities.
            bt.logging.trace(
                f"Blacklisting un-registered hotkey {synapse.dendrite.hotkey}"
            )
            return True, "Unrecognized hotkey"
        uid = self.metagraph.hotkeys.index(synapse.dendrite.hotkey)
        if self.config.blacklist.force_validator_permit:
            # If the config is set to force validator permit, then we should only allow requests from validators.
            # We also check if the stake is greater than 10_000, which is the minimum stake to not be blacklisted.
            if (
                not self.metagraph.validator_permit[uid]
                or self.metagraph.stake[uid] < 10_000
            ):
                bt.logging.warning(
                    f"Blacklisting a request from non-validator hotkey {synapse.dendrite.hotkey}"
                )
                return True, "Non-validator hotkey"

        bt.logging.trace(
            f"Not Blacklisting recognized hotkey {synapse.dendrite.hotkey}"
        )
        return False, "Hotkey recognized!"

    async def priority(self, synapse: JobSubmissionSynapse) -> float:
        caller_uid = self.metagraph.hotkeys.index(
            synapse.dendrite.hotkey
        )  # Get the caller index.
        priority = float(
            self.metagraph.S[caller_uid]
        )  # Return the stake as the priority.
        bt.logging.trace(
            f"Prioritizing {synapse.dendrite.hotkey} with value: ", priority
        )
        return priority


class SimulationManager:
    def __init__(self, pdb_id: str, output_dir: str) -> None:
        self.pdb_id = pdb_id
        self.state: str = None
        self.state_file_name = f"{pdb_id}_state.txt"

        self.output_dir = output_dir
        self.start_time = time.time()

    def create_empty_file(self, file_path: str):
        # For mocking
        with open(file_path, "w") as f:
            pass

    def run(
        self,
        md_inputs: Dict,
        commands: Dict,
        suppress_cmd_output: bool = True,
        mock: bool = False,
    ):
        """run method to handle the processing of generic simulations.

        Args:
            md_inputs (Dict): input files from the validator
            commands (Dict): dictionary where state as the key and the commands as the value
            suppress_cmd_output (bool, optional): Defaults to True.
            mock (bool, optional): mock for debugging. Defaults to False.
        """
        _start = time.time()
        bt.logging.info(
            f"Running simulation for protein: {self.pdb_id} with files {md_inputs.keys()}"
        )

        # Make sure the output directory exists and if not, create it
        check_if_directory_exists(output_directory=self.output_dir)
        os.chdir(self.output_dir)  # TODO: will this be a problem with many processes?

        # The following files are required for GROMACS simulations and are recieved from the validator
        for filename, content in md_inputs.items():
            # Write the file to the output directory
            with open(filename, "w") as file:
                bt.logging.info(f"\nWriting {filename} to {self.output_dir}")
                file.write(content)

        for state, commands in commands.items():
            bt.logging.info(f"Running {state} commands")
            with open(self.state_file_name, "w") as f:
                f.write(f"{state}\n")

            run_cmd_commands(
                commands=commands, suppress_cmd_output=suppress_cmd_output, verbose=True
            )

            if mock:
                bt.logging.warning("Running in mock mode, creating fake files...")
                for ext in ["tpr", "xtc", "edr", "cpt"]:
                    self.create_empty_file(
                        os.path.join(self.output_dir, f"{state}.{ext}")
                    )

        _smlt_time = time.time() - _start
        bt.logging.success(f"✅ Finished simulation in {_smlt_time} seconds for protein: {self.pdb_id} ✅")

        state = "finished"
        with open(self.state_file_name, "w") as f:
            f.write(f"{state}\n")

    def get_state(self) -> str:
        """get_state reads a txt file that contains the current state of the simulation"""
        with open(os.path.join(self.output_dir, self.state_file_name), "r") as f:
            lines = f.readlines()
            return (
                lines[-1].strip() if lines else None
            )  # return the last line of the file


class MockSimulationManager(SimulationManager):
    def __init__(self, pdb_id: str, output_dir: str) -> None:
        super().__init__(pdb_id=pdb_id)
        self.required_values = set(["init", "wait", "finished"])
        self.output_dir = output_dir

    def run(self, total_wait_time: int = 1):
        start_time = time.time()

        bt.logging.debug(f"✅ MockSimulationManager.run is running ✅")
        check_if_directory_exists(output_directory=self.output_dir)

        store = os.path.join(self.output_dir, self.state_file_name)
        states = ["init", "wait", "finished"]

        intermediate_interval = total_wait_time / len(states)

        for state in states:
            bt.logging.info(f"Running state: {state}")
            state_time = time.time()
            with open(store, "w") as f:
                f.write(f"{state}\n")

            time.sleep(intermediate_interval)
            bt.logging.info(f"Total state_time: {time.time() - state_time}")

        bt.logging.warning(f"Total run method time: {time.time() - start_time}")
