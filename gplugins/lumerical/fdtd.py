"""Write Sparameters with Lumerical FDTD"""
from __future__ import annotations

import multiprocessing
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

import gdsfactory as gf
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml
from gdsfactory.component import Component
from gdsfactory.config import __version__, logger
from gdsfactory.pdk import get_layer_stack
from gdsfactory.technology import LayerStack

from gplugins.common.utils.get_sparameters_path import (
    get_sparameters_path_lumerical as get_sparameters_path,
)
from gplugins.lumerical.convergence_settings import (
    LUMERICAL_FDTD_CONVERGENCE_SETTINGS,
    ConvergenceSettingsLumericalFdtd,
)
from gplugins.lumerical.simulation_settings import (
    SIMULATION_SETTINGS_LUMERICAL_FDTD,
    SimulationSettingsLumericalFdtd,
)
from gplugins.lumerical.utils import draw_geometry, layerstack_to_lbr

try:
    import lumapi
except ModuleNotFoundError as e:
    print(
        "Cannot import lumapi (Python Lumerical API). "
        "You can add set the PYTHONPATH variable or add it with `sys.path.append()`"
    )
    raise e
except OSError as e:
    raise e

if TYPE_CHECKING:
    from gdsfactory.typings import PathType

run_false_warning = """
You have passed run=False to debug the simulation

run=False returns the simulation session for you to debug and make sure it is correct

To compute the Sparameters you need to pass run=True
"""

um = 1e-6
marker_list = [
    "o",
    "v",
    "^",
    "<",
    ">",
    "1",
    "2",
    "3",
    "4",
    "s",
    "p",
    "P",
    "*",
    "h",
    "+",
    "X",
    "D",
] * 10


def main():
    from functools import partial

    from gdsfactory.components.taper_cross_section import taper_cross_section

    xs_wg = partial(
        gf.cross_section.cross_section,
        layer=(1, 0),
        width=0.5,
    )

    xs_wg_wide = partial(
        gf.cross_section.cross_section,
        layer=(1, 0),
        width=2.0,
    )

    taper = taper_cross_section(
        cross_section1=xs_wg,
        cross_section2=xs_wg_wide,
        length=5,
        width_type="parabolic",
    )

    layer_map = {
        "si": "Si (Silicon) - Palik",
        "sio2": "SiO2 (Glass) - Palik",
        "sin": "Si3N4 (Silicon Nitride) - Phillip",
        "TiN": "TiN - Palik",
        "Aluminum": "Al (Aluminium) Palik",
    }
    from gdsfactory.technology.layer_stack import LayerLevel, LayerStack

    layerstack_lumerical2021 = LayerStack(
        layers={
            "clad": LayerLevel(
                name=None,
                layer=(99999, 0),
                thickness=3.0,
                thickness_tolerance=None,
                zmin=0.0,
                zmin_tolerance=None,
                material="sio2",
                sidewall_angle=0.0,
                sidewall_angle_tolerance=None,
                width_to_z=0.0,
                z_to_bias=None,
                mesh_order=9,
                layer_type="background",
                mode=None,
                into=None,
                resistivity=None,
                bias=None,
                derived_layer=None,
                info={},
                background_doping_concentration=None,
                background_doping_ion=None,
                orientation="100",
            ),
            "box": LayerLevel(
                name=None,
                layer=(99999, 0),
                thickness=3.0,
                thickness_tolerance=None,
                zmin=-3.0,
                zmin_tolerance=None,
                material="sio2",
                sidewall_angle=0.0,
                sidewall_angle_tolerance=None,
                width_to_z=0.0,
                z_to_bias=None,
                mesh_order=9,
                layer_type="background",
                mode=None,
                into=None,
                resistivity=None,
                bias=None,
                derived_layer=None,
                info={},
                background_doping_concentration=None,
                background_doping_ion=None,
                orientation="100",
            ),
            "core": LayerLevel(
                name=None,
                layer=(1, 0),
                thickness=0.22,
                thickness_tolerance=None,
                zmin=0.0,
                zmin_tolerance=None,
                material="si",
                sidewall_angle=10.0,
                sidewall_angle_tolerance=None,
                width_to_z=0.5,
                z_to_bias=None,
                mesh_order=2,
                layer_type="grow",
                mode=None,
                into=None,
                resistivity=None,
                bias=None,
                derived_layer=None,
                info={"active": True},
                background_doping_concentration=100000000000000.0,
                background_doping_ion="Boron",
                orientation="100",
            ),
            # KNOWN ISSUE: Lumerical 2021 version of Layer Builder does not support dopants in process file
        }
    )
    SIMULATION_SETTINGS_LUMERICAL_FDTD.port_translation = 1.0
    sim = LumericalFdtdSimulation(
        component=taper,
        material_map=layer_map,
        layerstack=layerstack_lumerical2021,
        convergence_settings=LUMERICAL_FDTD_CONVERGENCE_SETTINGS,
        simulation_settings=SIMULATION_SETTINGS_LUMERICAL_FDTD,
        hide=False,
        dirpath="/root/PycharmProjects/gdsfactory_sean/gplugins/gplugins/lumerical/tests",
        run_port_convergence=True,
        run_mesh_convergence=False,
    )

    sp = sim.write_sparameters(overwrite=True)
    print(sp)
    print("Done")


class LumericalFdtdSimulation:
    """
    Lumerical FDTD simulation

    Set up FDTD simulation based on component geometry and simulation settings. Optionally, run convergence.

    Attributes:
        component: Component geometry to simulate
        material_map: Map of PDK materials to Lumerical materials
        layerstack: PDK layerstack
        session: Lumerical session
        simulation_settings: EME simulation settings
        convergence_settings: EME convergence settings
        dirpath: Directory where simulation files are saved
        filepath_npz: S-parameter filepath (npz)
        filepath_fsp: FDTD simulation filepath (fsp)
        mesh_convergence_data: Mesh convergence results

    """

    def __init__(
        self,
        component: Component,
        material_map: dict[str, str],
        layerstack: LayerStack | None = None,
        session: lumapi.FDTD | None = None,
        simulation_settings: SimulationSettingsLumericalFdtd = SIMULATION_SETTINGS_LUMERICAL_FDTD,
        convergence_settings: ConvergenceSettingsLumericalFdtd = LUMERICAL_FDTD_CONVERGENCE_SETTINGS,
        dirpath: PathType | None = "",
        hide: bool = True,
        run_mesh_convergence: bool = False,
        run_port_convergence: bool = False,
        run_field_intensity_convergence: bool = False,
        xmargin: float = 0,
        ymargin: float = 0,
        xmargin_left: float = 0,
        xmargin_right: float = 0,
        ymargin_top: float = 0,
        ymargin_bot: float = 0,
        zmargin: float = 1.0,
        **settings,
    ):
        r"""Creates FDTD simulation for extracting s-parameters

        Your components need to have ports, that will extend over the PML.

        .. image:: https://i.imgur.com/dHAzZRw.png

        For your Fab technology you can overwrite

        - simulation_settings
        - dirpath
        - layerStack

        converts gdsfactory units (um) to Lumerical units (m)

        Disclaimer: This function tries to create a generalized FDTD simulation to extract Sparameters.
        It is hard to make a function that will fit all your possible simulation settings.
        You can use this function for inspiration to create your own.

        Args:
            component: Component to simulate.
            material_map: Map of PDK materials to Lumerical materials
            layerstack: PDK layerstack
            session: you can pass a session=lumapi.FDTD() or it will create one.
            simulation_settings: dataclass with all simulation_settings.
            convergence_settings: FDTD convergence settings
            dirpath: Directory where simulation files and sparams (.npz) are saved
                Defaults to active Pdk.sparameters_path.
            hide: Hide simulation if True, else show GUI
            run_mesh_convergence: If True, run sweep of mesh and monitor sparam convergence.
            run_port_convergence: If True, run port convergence where ports are resized based on E-field intensity
                threshold. Edges of the port must decay to this threshold.
            run_field_intensity_convergence: If True, run sweep of E-field intensity threshold vs. sparam convergence.
                Then, update the E-field intensity threshold to suit desired sparam convergence (sparam_diff).
            xmargin: left/right distance from component to PML.
            xmargin_left: left distance from component to PML.
            xmargin_right: right distance from component to PML.
            ymargin: left/right distance from component to PML.
            ymargin_top: top distance from component to PML.
            ymargin_bot: bottom distance from component to PML.
            zmargin: thickness for cladding above and below core.

        Keyword Args:
            background_material: for the background.
            port_margin: on both sides of the port width (um).
            port_height: port height (um).
            port_extension: port extension (um).
            mesh_accuracy: 2 (1: coarse, 2: fine, 3: superfine).
            wavelength_start: 1.2 (um).
            wavelength_stop: 1.6 (um).
            wavelength_points: 500.
            simulation_time: (s) related to max path length 3e8/2.4*10e-12*1e6 = 1.25mm.
            simulation_temperature: in kelvin (default = 300).
            frequency_dependent_profile: computes mode profiles for different wavelengths.
            field_profile_samples: number of wavelengths to compute field profile.


        .. code::

             top view
                  ________________________________
                 |                               |
                 | xmargin                       | port_extension
                 |<------>          port_margin ||<-->
              o2_|___________          _________||_o3
                 |           \        /          |
                 |            \      /           |
                 |             ======            |
                 |            /      \           |
              o1_|___________/        \__________|_o4
                 |   |                           |
                 |   |ymargin                    |
                 |   |                           |
                 |___|___________________________|

            side view
                  ________________________________
                 |                               |
                 |                               |
                 |                               |
                 |ymargin                        |
                 |<---> _____         _____      |
                 |     |     |       |     |     |
                 |     |     |       |     |     |
                 |     |_____|       |_____|     |
                 |       |                       |
                 |       |                       |
                 |       |zmargin                |
                 |       |                       |
                 |_______|_______________________|

        """
        if isinstance(dirpath, str):
            dirpath = Path(dirpath)
        self.dirpath = dirpath = dirpath or Path(__file__).resolve().parent

        self.convergence_settings = convergence_settings = (
            convergence_settings or LUMERICAL_FDTD_CONVERGENCE_SETTINGS
        )
        self.component = component = gf.get_component(component)
        sim_settings = dict(simulation_settings)

        self.layerstack = layer_stack = layerstack or get_layer_stack()

        layer_to_thickness = layer_stack.get_layer_to_thickness()
        layer_to_zmin = layer_stack.get_layer_to_zmin()

        if hasattr(component.info, "simulation_settings"):
            sim_settings |= component.info.simulation_settings
            logger.info(
                f"Updating {component.name!r} sim settings {component.simulation_settings}"
            )
        for setting in settings:
            if setting not in sim_settings:
                raise ValueError(
                    f"Invalid setting {setting!r} not in ({list(sim_settings.keys())})"
                )

        sim_settings.update(**settings)
        self.simulation_settings = ss = SimulationSettingsLumericalFdtd(**sim_settings)

        component_with_booleans = layer_stack.get_component_with_derived_layers(
            component
        )
        component_with_padding = gf.add_padding_container(
            component_with_booleans,
            default=0,
            top=ymargin or ymargin_top,
            bottom=ymargin or ymargin_bot,
            left=xmargin or xmargin_left,
            right=xmargin or xmargin_right,
        )

        component_extended = gf.components.extend_ports(
            component_with_padding, length=ss.distance_monitors_to_pml
        )

        ports = component.get_ports_list(port_type="optical")
        if not ports:
            raise ValueError(f"{component.name!r} does not have any optical ports")

        component_extended_beyond_pml = gf.components.extension.extend_ports(
            component=component_extended, length=ss.port_extension
        )
        component_extended_beyond_pml.name = "top"
        gdspath = component_extended_beyond_pml.write_gds()

        x_min = (component_extended.xmin - xmargin) * um
        x_max = (component_extended.xmax + xmargin) * um
        y_min = (component_extended.ymin - ymargin) * um
        y_max = (component_extended.ymax + ymargin) * um

        layers_thickness = [
            layer_to_thickness[layer]
            for layer in component_with_booleans.get_layers()
            if layer in layer_to_thickness
        ]
        if not layers_thickness:
            raise ValueError(
                f"no layers for component {component.get_layers()}"
                f"in layer stack {layer_stack}"
            )
        layers_zmin = [
            layer_to_zmin[layer]
            for layer in component_with_booleans.get_layers()
            if layer in layer_to_zmin
        ]
        component_thickness = max(layers_thickness)
        component_zmin = min(layers_zmin)

        z = (component_zmin + component_thickness) / 2 * um
        z_span = (2 * zmargin + component_thickness) * um

        x_span = x_max - x_min
        y_span = y_max - y_min

        sim_settings.update(dict(layer_stack=layer_stack.to_dict()))

        sim_settings = dict(
            simulation_settings=sim_settings,
            component=component.to_dict(),
            version=__version__,
        )

        logger.info(
            f"Simulation size = {x_span / um:.3f}, {y_span / um:.3f}, {z_span / um:.3f} um"
        )

        self.session = s = session or lumapi.FDTD(hide=hide)
        s.newproject()
        s.selectall()
        s.deleteall()

        material_name_to_lumerical_new = material_map or {}
        material_name_to_lumerical = ss.material_name_to_lumerical.copy()
        material_name_to_lumerical.update(**material_name_to_lumerical_new)
        self.material_map = material_name_to_lumerical

        s.addfdtd(
            dimension="3D",
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
            z=z,
            z_span=z_span,
            mesh_accuracy=ss.mesh_accuracy,
            use_early_shutoff=True,
            simulation_time=ss.simulation_time,
            simulation_temperature=ss.simulation_temperature,
        )

        ### Create Layer Builder object and insert geometry
        process_file_path = layerstack_to_lbr(
            material_name_to_lumerical, layer_stack, dirpath
        )
        draw_geometry(s, gdspath, process_file_path)

        # Fit material models
        for layer_name in layerstack.to_dict():
            s.select("layer group")
            material_name = s.getlayer(layer_name, "pattern material")
            try:
                s.setmaterial(material_name, "wavelength min", ss.wavelength_start * um)
                s.setmaterial(material_name, "wavelength max", ss.wavelength_stop * um)
                s.setmaterial(material_name, "tolerance", ss.material_fit_tolerance)
            except lumapi.LumApiError:
                logger.warning(
                    f"Material {material_name} cannot be found in database, skipping material fit."
                )

        # Get current FDTD region bounds. When adding ports, if the port is outside the FDTD region, correct FDTD bounds
        fdtd_xmin = s.getnamed("FDTD", "x min")
        fdtd_ymin = s.getnamed("FDTD", "y min")
        fdtd_zmin = s.getnamed("FDTD", "z min")
        fdtd_xmax = s.getnamed("FDTD", "x max")
        fdtd_ymax = s.getnamed("FDTD", "y max")
        fdtd_zmax = s.getnamed("FDTD", "z max")

        # Add ports
        for i, port in enumerate(ports):
            zmin = layer_to_zmin[port.layer]
            thickness = layer_to_thickness[port.layer]
            z = (zmin + thickness) / 2
            zspan = 2 * ss.port_margin + thickness

            s.addport()
            p = f"FDTD::ports::port {i + 1}"
            s.setnamed(p, "z", z * um)
            s.setnamed(p, "z span", zspan * um)
            s.setnamed(p, "frequency dependent profile", ss.frequency_dependent_profile)
            s.setnamed(p, "number of field profile samples", ss.field_profile_samples)

            deg = port.orientation
            if -45 <= deg <= 45:
                direction = "Backward"
                injection_axis = "x-axis"
                s.setnamed(p, "x", (port.x + ss.port_translation) * um)
                s.setnamed(p, "y", port.y * um)
                dxp = 0
                dyp = 2 * ss.port_margin + port.width
            elif 45 < deg < 90 + 45:
                direction = "Backward"
                injection_axis = "y-axis"
                s.setnamed(p, "x", port.x * um)
                s.setnamed(p, "y", (port.y - ss.port_translation) * um)
                dxp = 2 * ss.port_margin + port.width
                dyp = 0
            elif 90 + 45 < deg < 180 + 45:
                direction = "Forward"
                injection_axis = "x-axis"
                s.setnamed(p, "x", (port.x - ss.port_translation) * um)
                s.setnamed(p, "y", port.y * um)
                dxp = 0
                dyp = 2 * ss.port_margin + port.width
            elif 180 + 45 < deg < 180 + 45 + 90:
                direction = "Forward"
                injection_axis = "y-axis"
                s.setnamed(p, "x", port.x * um)
                s.setnamed(p, "y", (port.y + ss.port_translation) * um)
                dxp = 2 * ss.port_margin + port.width
                dyp = 0

            else:
                raise ValueError(
                    f"port {port.name!r} orientation {port.orientation} is not valid"
                )

            s.setnamed(p, "direction", direction)
            s.setnamed(p, "injection axis", injection_axis)
            s.setnamed(p, "y span", dyp * um)
            s.setnamed(p, "x span", dxp * um)

            # Correct FDTD bounds if ports are outside FDTD region
            port_xmin = s.getnamed(p, "x min")
            port_ymin = s.getnamed(p, "y min")
            port_zmin = s.getnamed(p, "z min")
            port_xmax = s.getnamed(p, "x max")
            port_ymax = s.getnamed(p, "y max")
            port_zmax = s.getnamed(p, "z max")
            if port_xmin < fdtd_xmin:
                fdtd_xmin = port_xmin - ss.port_margin * um
                s.setnamed("FDTD", "x min", fdtd_xmin)
            if port_ymin < fdtd_ymin:
                fdtd_ymin = port_ymin - ss.port_margin * um
                s.setnamed("FDTD", "y min", fdtd_ymin)
            if port_zmin < fdtd_zmin:
                fdtd_zmin = port_zmin - ss.port_margin * um
                s.setnamed("FDTD", "z min", fdtd_zmin)
            if port_xmax > fdtd_xmax:
                fdtd_xmax = port_xmax + ss.port_margin * um
                s.setnamed("FDTD", "x max", fdtd_xmax)
            if port_ymax > fdtd_ymax:
                fdtd_ymax = port_ymax + ss.port_margin * um
                s.setnamed("FDTD", "y max", fdtd_ymax)
            if port_zmax > fdtd_zmax:
                fdtd_zmax = port_zmax + ss.port_margin * um
                s.setnamed("FDTD", "z max", fdtd_zmax)

            s.setnamed(p, "name", port.name)

            logger.info(
                f"port {p} {port.name!r}: at ({port.x}, {port.y}, 0)"
                f"size = ({dxp}, {dyp}, {zspan})"
            )

        s.setglobalsource("wavelength start", ss.wavelength_start * um)
        s.setglobalsource("wavelength stop", ss.wavelength_stop * um)
        s.setnamed("FDTD::ports", "monitor frequency points", ss.wavelength_points)

        # Add base sparam sweep
        s.deletesweep("s-parameter sweep")
        s.addsweep(3)
        s.setsweep("s-parameter sweep", "Excite all ports", 1)
        s.setsweep("S sweep", "auto symmetry", True)

        # Save simulation and settings
        self.filepath_npz = get_sparameters_path(
            component=component,
            dirpath=dirpath,
            layer_stack=layer_stack,
            **settings,
        )
        filepath_dat = self.filepath_npz.with_suffix(".dat")
        filepath_sim_settings = filepath_dat.with_suffix(".yml")
        self.filepath_fsp = filepath_fsp = filepath_dat.with_suffix(".fsp")

        s.save(str(filepath_fsp))
        filepath_sim_settings.write_text(yaml.dump(sim_settings))

        # Run convergence if specified
        if run_field_intensity_convergence:
            self.field_intensity_convergence_data = (
                self.update_field_intensity_threshold(plot=not hide)
            )

        if run_port_convergence:
            self.update_port_convergence(verbose=not hide)

        if run_mesh_convergence:
            self.mesh_convergence_data = self.update_mesh_convergence(
                verbose=not hide, plot=not hide
            )

    def write_sparameters(
        self,
        overwrite: bool = False,
        delete_fsp_files: bool = False,
        plot: bool = False,
    ) -> pd.DataFrame:
        """
        Run s-parameter simulation; write s-parameters to npz, csv, dat files; or read s-parameters from csv.

        If s-parameter data saved in csv file as pandas.DataFrame, retrieve the data and return.
        Else, run s-parameter simulation, extract s-parameters, and save the following:
        - YAML simulation setup (.yml)
        - s-parameters (.npz, .csv, .dat)
        - Plot of s-parameters, optional (.png)

        Parameters:
            overwrite: If True, overwrites s-parameter files
            delete_fsp_files: If True, deletes s-parameter simulation files
            plot: If True, plot s-parameters and save plot (.png)

        Returns:
            S-parameters vs wavelength
            | wavelength | S11     | S12     | ...
            | float      | complex | complex | ...
            | um         |         |         | ...
        """
        s = self.session

        filepath = self.filepath_npz.with_suffix(".dat")
        filepath_sim_settings = filepath.with_suffix(".yml")
        filepath_csv = self.filepath_npz.with_suffix(".csv")
        fspdir = filepath.parent / f"{filepath.stem}_s-parametersweep"

        if filepath_csv.exists() and not overwrite:
            logger.info(f"Reading Sparameters from {filepath_csv!r}")
            sparam_data = pd.read_csv(filepath_csv, index_col=0)

            return sparam_data

        start = time.time()
        s.save()
        # Add base sparam sweep
        s.deletesweep("s-parameter sweep")
        s.addsweep(3)
        s.setsweep("s-parameter sweep", "Excite all ports", 1)
        s.setsweep("S sweep", "auto symmetry", True)
        s.runsweep()
        sp = s.getsweepresult("s-parameter sweep", "S parameters")
        s.exportsweep("s-parameter sweep", str(filepath))
        logger.info(f"Writing Sparameters to {str(filepath)!r}")

        sp["wavelengths"] = sp.pop("lambda").flatten() / um
        np.savez_compressed(self.filepath_npz, **sp)
        logger.info(f"Writing Sparameters to {self.filepath_npz.absolute()!r}")

        end = time.time()
        sim_settings = self.simulation_settings.model_dump()
        sim_settings.update(compute_time_seconds=end - start)
        sim_settings.update(compute_time_minutes=(end - start) / 60)
        filepath_sim_settings.write_text(yaml.dump(sim_settings))
        if delete_fsp_files and fspdir.exists():
            shutil.rmtree(fspdir)
            logger.info(
                f"deleting simulation files in {str(fspdir)!r}. "
                "To keep them, use delete_fsp_files=False flag"
            )

        sparams = {k: sp[k] for k in sp["Lumerical_dataset"]["attributes"]}
        sparams["wavelength"] = list(sp["wavelengths"])
        sparam_data = pd.DataFrame(sparams)
        sparam_data.to_csv(filepath_csv)
        logger.info(f"Writing Sparameters to {filepath_csv!r}")

        if plot:
            plt.figure()
            columns = list(sparam_data.columns)
            for i in range(0, len(columns)):
                if not columns[i] == "wavelength":
                    plt.plot(
                        sparam_data.loc[:, "wavelength"],
                        abs(sparam_data.loc[:, columns[i]]) ** 2,
                        label=f"|{columns[i]}|^2",
                        marker=marker_list[i],
                    )
            plt.xlabel("Wavelength (um)")
            plt.ylabel("Magnitude")
            plt.title("S-Parameters")
            plt.grid("on")
            plt.legend()
            plt.tight_layout()
            plt.savefig(str(self.dirpath / f"{self.component.name}_s-parameters.png"))

        return sparam_data

    def update_port_convergence(
        self,
        port_modes: dict | None = None,
        mesh_accuracy: int = 4,
        verbose: bool = False,
    ):
        """Update size of ports based on mode spec

        Args:
            port_modes: Map between port name and target mode number. Ex. The following shows
                        how port 1 is targeting mode 2 and port 2 is targeting mode 1 (fundamental)
                        {
                            'port 1': 2,
                            'port 2': 1,
                            .
                            .
                        }
            mesh_accuracy: Mesh accuracy used for port E-field calculations
            verbose: Print debug messages
        """
        port_modes = port_modes or {}

        s = self.session
        threshold = self.convergence_settings.port_field_intensity_threshold

        # Get number of ports
        s.groupscope("::model::FDTD::ports")
        s.selectall()
        s.groupscope("::model")

        # Ensure mesh accuracy is medium-high to get accurate port E-field calculations
        orig_mesh_accuracy = s.getnamed("FDTD", "mesh accuracy")
        s.setnamed("FDTD", "mesh accuracy", mesh_accuracy)

        # Iterate through ports
        for port in self.component.get_ports():
            # Set port size 5x existing size to calculate E field intensity properly and ensure FDTD region encapsulates port
            # NOTE: Port edges have a boundary condition that ensures the field decays to near zero. So, even if port is
            # sized just larger than waveguide, E field does not decay properly
            s.select(f"FDTD::ports::{port.name}")
            s.set("x span", s.get("x span") * 5)
            s.set("y span", s.get("y span") * 5)
            s.set("z span", s.get("z span") * 5)

            port_xmin = s.get("x min")
            port_xmax = s.get("x max")
            port_ymin = s.get("y min")
            port_ymax = s.get("y max")
            port_zmin = s.get("z min")
            port_zmax = s.get("z max")

            s.select("FDTD")
            fdtd_zmin = s.get("z min")
            fdtd_zmax = s.get("z max")
            fdtd_xmin = s.get("x min")
            fdtd_xmax = s.get("x max")
            fdtd_ymin = s.get("y min")
            fdtd_ymax = s.get("y max")

            if port_xmin < fdtd_xmin:
                s.set("x min", port_xmin)
            if port_xmax > fdtd_xmax:
                s.set("x max", port_xmax)
            if port_ymin < fdtd_ymin:
                s.set("y min", port_ymin)
            if port_ymax > fdtd_ymax:
                s.set("y max", port_ymax)
            if port_zmin < fdtd_zmin:
                s.set("z min", port_zmin)
            if port_zmax > fdtd_zmax:
                s.set("z max", port_zmax)

            converged = False
            while not converged:
                # Get FDTD region z min, z max, x min, x max, y min, y max
                # Updating port sizes may affect FDTD region size
                s.select("FDTD")
                fdtd_zmin = s.get("z min")
                fdtd_zmax = s.get("z max")
                fdtd_xmin = s.get("x min")
                fdtd_xmax = s.get("x max")
                fdtd_ymin = s.get("y min")
                fdtd_ymax = s.get("y max")

                # Get target mode number and set mode number in port
                port_mode = port_modes.get(port.name, 1)  # default is fundamental mode
                s.select(f"FDTD::ports::{port.name}")
                s.set("mode selection", "user select")
                s.set("selected mode numbers", port_mode)
                s.updateportmodes(port_mode)

                # Get E field intensity
                s.eval(
                    f'select("FDTD::ports::{port.name}");'
                    + f'mode_profiles=getresult("FDTD::ports::{port.name}","mode profiles");'
                    + f"E=mode_profiles.E{port_mode}; x=mode_profiles.x; y=mode_profiles.y; z=mode_profiles.z;"
                    + f'?"Selected pin: {port.name}";'
                )
                E = s.getv("E")
                x = s.getv("x")
                y = s.getv("y")
                z = s.getv("z")

                # Check if at least two dimensions are not singular
                dim = 0
                if not isinstance(x, float):
                    dim += 1
                if not isinstance(y, float):
                    dim += 1
                if not isinstance(z, float):
                    dim += 1
                if dim < 2:
                    raise TypeError(
                        f"Port {port.name} mode profile is missing a dimension (only single dimension). Check port orientation."
                    )

                # To get E field intensity, need to find port orientation
                # The E field intensity data depends on injection axis
                s.select(f"FDTD::ports::{port.name}")
                inj_axis = s.get("injection axis")
                if inj_axis == "x-axis":
                    Efield_xyz = np.array(E[0, :, :, 0, :])
                elif inj_axis == "y-axis":
                    Efield_xyz = np.array(E[:, 0, :, 0, :])

                Efield_intensity = np.empty([Efield_xyz.shape[0], Efield_xyz.shape[1]])
                for a in range(0, Efield_xyz.shape[0]):
                    for b in range(0, Efield_xyz.shape[1]):
                        Efield_intensity[a, b] = (
                            abs(Efield_xyz[a, b, 0]) ** 2
                            + abs(Efield_xyz[a, b, 1]) ** 2
                            + abs(Efield_xyz[a, b, 2]) ** 2
                        )

                # Get max E field intensity along x/y axis
                Efield_intensity_xy = np.empty([Efield_xyz.shape[0]])
                for a in range(0, Efield_xyz.shape[0]):
                    Efield_intensity_xy[a] = max(Efield_intensity[a, :])

                # Get max E field intensity along z axis
                Efield_intensity_z = np.empty([Efield_xyz.shape[1]])
                for b in range(0, Efield_xyz.shape[1]):
                    Efield_intensity_z[b] = max(Efield_intensity[:, b])

                # Get initial z min and z max for expansion reference
                # Get initial z span;  this will be used to expand ports
                s.select(f"FDTD::ports::{port.name}")
                port_z_min = s.get("z min")
                port_z_max = s.get("z max")
                port_z_span = s.get("z span")

                # If all E field intensities > threshold, expand z span of port by initial z span
                # Else, set z min and z max to locations where E field intensities decay below threshold
                indexes = np.argwhere(Efield_intensity_z > threshold)
                if len(indexes) == 0:
                    min_index = 0
                    max_index = len(Efield_intensity_z) - 1
                else:
                    min_index, max_index = int(min(indexes)), int(max(indexes))

                if min_index == 0:
                    s.set("z min", port_z_min - port_z_span / 2)
                    converged_zmin = False
                else:
                    s.set("z min", z[min_index - 1])
                    converged_zmin = True

                if max_index == (len(Efield_intensity_z) - 1):
                    s.set("z max", port_z_max + port_z_span / 2)
                    converged_zmax = False
                else:
                    s.set("z max", z[max_index + 1])
                    converged_zmax = True

                if verbose:
                    logger.info(
                        f"port {port.name}, mode {port_mode} field decays at: {z[max_index]}, {z[min_index]} microns"
                    )

                # Get initial x/y min and x/y max for expansion reference
                # Get initial x/y span;  this will be used to expand ports
                s.select(f"FDTD::ports::{port.name}")
                if inj_axis == "x-axis":
                    port_xy_min = s.get("y min")
                    port_xy_max = s.get("y max")
                    port_xy_span = s.get("y span")

                    # If all E field intensities > threshold, expand x/y span of port by initial x/y span
                    # Else, set x/y min and x/y max to locations where E field intensities decay below threshold
                    indexes = np.argwhere(Efield_intensity_xy > threshold)
                    if len(indexes) == 0:
                        min_index = 0
                        max_index = len(Efield_intensity_xy) - 1
                    else:
                        min_index, max_index = int(min(indexes)), int(max(indexes))

                    if min_index == 0:
                        s.set("y min", port_xy_min - port_xy_span / 2)
                        converged_ymin = False
                    else:
                        s.set("y min", y[min_index - 1])
                        converged_ymin = True

                    if max_index == (len(Efield_intensity_xy) - 1):
                        s.set("y max", port_xy_max + port_xy_span / 2)
                        converged_ymax = False
                    else:
                        s.set("y max", y[max_index + 1])
                        converged_ymax = True

                    if verbose:
                        logger.info(
                            f"port {port.name}, mode {port_mode} field decays at: {y[max_index]}, {y[min_index]} microns"
                        )

                    converged = (
                        converged_ymax
                        & converged_ymin
                        & converged_zmax
                        & converged_zmin
                    )

                elif inj_axis == "y-axis":
                    port_xy_min = s.get("x min")
                    port_xy_max = s.get("x max")
                    port_xy_span = s.get("x span")

                    # If all E field intensities > threshold, expand x/y span of port by initial x/y span
                    # Else, set x/y min and x/y max to locations where E field intensities decay below threshold
                    indexes = np.argwhere(Efield_intensity_xy > threshold)
                    if len(indexes) == 0:
                        min_index = 0
                        max_index = len(Efield_intensity_xy) - 1
                    else:
                        min_index, max_index = int(min(indexes)), int(max(indexes))

                    if min_index == 0:
                        s.set("x min", port_xy_min - port_xy_span / 2)
                        converged_xmin = False
                    else:
                        s.set("x min", x[min_index - 1])
                        converged_xmin = True

                    if max_index == (len(Efield_intensity_xy) - 1):
                        s.set("x max", port_xy_max + port_xy_span / 2)
                        converged_xmax = False
                    else:
                        s.set("x max", x[max_index + 1])
                        converged_xmax = True

                    if verbose:
                        logger.info(
                            f"port {port.name}, mode {port_mode} field decays at: {x[max_index]}, {x[min_index]} microns"
                        )

                    converged = (
                        converged_xmax
                        & converged_xmin
                        & converged_zmax
                        & converged_zmin
                    )

                # If port z min < FDTD z min or port z max > FDTD z max,
                # update FDTD z min or max to encapsulate ports
                # If port x/y min < FDTD x/y min or port x/y max > FDTD x/y max,
                # update FDTD x/y min or max to encapsulate ports
                s.select(f"FDTD::ports::{port.name}")
                port_z_min = s.get("z min")
                port_z_max = s.get("z max")
                port_x_min = s.get("x min")
                port_x_max = s.get("x max")
                port_y_min = s.get("y min")
                port_y_max = s.get("y max")
                s.select("FDTD")
                if port_z_min < fdtd_zmin:
                    s.set("z min", port_z_min)
                if port_x_min < fdtd_xmin:
                    s.set("x min", port_x_min)
                if port_y_min < fdtd_ymin:
                    s.set("y min", port_y_min)
                if port_z_max > fdtd_zmax:
                    s.set("z max", port_z_max)
                if port_x_max > fdtd_xmax:
                    s.set("x max", port_x_max)
                if port_y_max > fdtd_ymax:
                    s.set("y max", port_y_max)

        # Iterate through ports and set FDTD extents to maximum extents of ports
        s.select("FDTD")
        xmin = s.get("x max")
        xmax = s.get("x min")
        ymin = s.get("y max")
        ymax = s.get("y min")
        zmin = s.get("z max")
        zmax = s.get("z min")
        for port in self.component.get_ports():
            s.select(f"FDTD::ports::{port.name}")
            port_ymin = s.get("y min")
            port_ymax = s.get("y max")
            port_xmin = s.get("x min")
            port_xmax = s.get("x max")
            port_zmin = s.get("z min")
            port_zmax = s.get("z max")
            if port_xmin < xmin:
                xmin = port_xmin
            if port_ymin < ymin:
                ymin = port_ymin
            if port_zmin < zmin:
                zmin = port_zmin
            if port_xmax > xmax:
                xmax = port_xmax
            if port_ymax > ymax:
                ymax = port_ymax
            if port_zmax > zmax:
                zmax = port_zmax
        s.select("FDTD")
        s.set("x min", xmin - self.simulation_settings.port_margin * um)
        s.set("y min", ymin - self.simulation_settings.port_margin * um)
        s.set("z min", zmin - self.simulation_settings.port_margin * um)
        s.set("x max", xmax + self.simulation_settings.port_margin * um)
        s.set("y max", ymax + self.simulation_settings.port_margin * um)
        s.set("z max", zmax + self.simulation_settings.port_margin * um)

        # Restore original mesh accuracy
        s.set("mesh accuracy", orig_mesh_accuracy)
        s.save()

    def update_mesh_convergence(
        self,
        max_mesh_accuracy: int = 7,
        wavl_points: int = 1,
        cpu_usage_percent: float = 1,
        min_cpus_per_sim: int = 8,
        verbose: bool = False,
        plot: bool = False,
    ) -> pd.DataFrame:
        """
        Run mesh convergence and update mesh in base simulation.

        - Creates convergence directory to store simulations and results
        - Saves mesh convergence data in csv

        Parameters:
            max_mesh_accuracy: Maximum mesh accuracy to consider. Minimum is 3.
            wavl_points: Number of wavelength points to consider
            cpu_usage_percent: 1.0 = Use 100% of computing resources. 0.5 = Use 50% of computing resources.
                                Always rounds down to conserve computing resources for others.
            min_cpus_per_sim: Minimum cores used per simulation.
            verbose: If True, print debug messages.
            plot: If True, plot and save mesh convergence results. Plots s-parameter variation between current mesh level
                  and previous two mesh levels.

        Returns:
            Convergence data
            | mesh_accuracy | wavelength | S11      | S12 ...
            | int           | np.array   | np.array | np.array ...

        """
        if max_mesh_accuracy < 3:
            logger.warning(
                "Maximum mesh accuracy is less than 3. Setting maximum mesh accuracy to 3.\n"
                + "Maximum mesh accuracy must be greater than 3 to compare mesh levels for convergence."
            )
            max_mesh_accuracy = 3

        s = self.session
        cs = self.convergence_settings
        ss = self.simulation_settings

        # Set base simulation settings
        s.select("FDTD::ports")
        orig_wavl_points = s.get("monitor frequency points")
        s.set("monitor frequency points", wavl_points)

        # Set resources
        total_cpus = multiprocessing.cpu_count()
        cpus_free = int(np.floor(total_cpus * cpu_usage_percent))
        capacity = int(np.floor(cpus_free / min_cpus_per_sim)) or 1
        cpus_per_sim = min_cpus_per_sim if capacity > 1 else cpus_free
        s.setresource("FDTD", 1, "processes", cpus_per_sim)
        s.setresource("FDTD", 1, "capacity", capacity)
        if verbose:
            logger.info(
                f"Using {cpus_per_sim} cores per simulation with {capacity} simulations running simultaneously."
            )

        # Create directory for convergence sims and results
        p = self.dirpath / f"{self.component.name}_convergence"
        p.mkdir(parents=True, exist_ok=True)

        # Create convergence sims
        mesh_accuracies = []
        convergence_sims = []
        sparam_sims = []
        for mesh_accuracy in range(1, max_mesh_accuracy + 1):
            s.select("FDTD")
            s.set("mesh accuracy", mesh_accuracy)
            base_filename = f"{self.component.name}_mesh-accuracy-{mesh_accuracy}"

            convergence_sims.append(str(p / f"{base_filename}.fsp"))
            mesh_accuracies.append(mesh_accuracy)

            s.save(convergence_sims[-1])
            s.savesweep()
            sparam_dir = p / f"{base_filename}_s-parametersweep"
            for f in list(sparam_dir.glob("*.fsp")):
                sparam_sims.append(str(f))

        # Create and run job list for sparams
        s.load(str(self.filepath_fsp))
        s.clearjobs()
        for f in sparam_sims:
            s.addjob(f)
        if verbose:
            logger.info("Running jobs...")
        s.runjobs()

        # Collect sparam results
        sparams = {
            "mesh_accuracy": [],
            "wavelength": [],
        }
        for i in range(0, len(convergence_sims)):
            s.load(convergence_sims[i])
            try:
                s.loadsweep()

                # Get sparam data
                data = s.getsweepresult("s-parameter sweep", "S parameters")
                # Add wavelength data
                sparams["wavelength"].append(data["lambda"][0:wavl_points])
                sparams["mesh_accuracy"].append(mesh_accuracies[i])

                sparam_keys = data["Lumerical_dataset"]["attributes"]
                for sparam in sparam_keys:
                    if sparam not in sparams:
                        sparams[sparam] = [abs(data[sparam][0:wavl_points]) ** 2]
                    else:
                        sparams[sparam].append(abs(data[sparam][0:wavl_points]) ** 2)
            except lumapi.LumApiError as err:
                logger.warning(
                    f"{err} | Failed to load sparam data from {s.filebasename(s.currentfilename())}.fsp"
                )

        # Get mesh accuracy where sparams converge
        converged = False
        sparam_variation = []
        for i in range(2, len(sparams["mesh_accuracy"])):
            sparam_diffs = []
            for k, v in sparams.items():
                if not k == "mesh_accuracy" and not k == "wavelength":
                    # Get max variation in sparam from current entry to previous 2 entries
                    sparam_diffs.append(max(abs(v[i] - v[i - 1])))
                    sparam_diffs.append(max(abs(v[i] - v[i - 2])))

            # Check if sparams have converged
            sparam_variation.append(max(sparam_diffs))
            if max(sparam_diffs) < cs.sparam_diff and not converged:
                converged = True
                ss.mesh_accuracy = sparams["mesh_accuracy"][i]
                s.load(str(self.filepath_fsp))
                s.select("FDTD")
                s.set("mesh accuracy", ss.mesh_accuracy)
                s.save()
                if verbose:
                    logger.info(
                        f"Mesh convergence succeeded. Setting mesh accuracy to {ss.mesh_accuracy}."
                    )

        if plot:
            plt.figure()
            plt.plot(sparams["mesh_accuracy"][2:], sparam_variation)
            plt.title("Mesh Convergence")
            plt.ylabel("S-Parameter Variation")
            plt.xlabel("Mesh Accuracy")
            plt.grid("on")
            plt.tight_layout()
            plt.savefig(str(p / f"{self.component.name}_fdtd_mesh_convergence.png"))

        # If not converged, set to maximum mesh accuracy
        if not converged:
            ss.mesh_accuracy = max_mesh_accuracy
            s.load(str(self.filepath_fsp))
            s.select("FDTD")
            s.set("mesh accuracy", ss.mesh_accuracy)
            s.save()
            if verbose:
                logger.warning(
                    f"Mesh convergence failed. Setting mesh accuracy to {max_mesh_accuracy}."
                )

        # Restore original sim settings
        s.select("FDTD::ports")
        s.set("monitor frequency points", orig_wavl_points)

        convergence_data = pd.DataFrame(sparams)
        convergence_data.to_csv(
            str(p / f"{self.component.name}_fdtd_mesh_convergence.csv")
        )
        return convergence_data

    def update_field_intensity_threshold(
        self,
        port_modes: dict | None = None,
        mesh_accuracy: int = 6,
        wavl_points: int = 1,
        plot: bool = False,
    ) -> pd.DataFrame:
        """
        Update port field intensity threshold based on sweep of field intensity and sparam convergence.

        Saves convergence data as pd.DataFrame in csv. Optionally, plots and saves png.

        Parameters:
            port_modes: Map of port name to target mode in port
            mesh_accuracy: Mesh accuracy to perform get mode profiles. Ensure this is high for accurate mode profiles.
            wavl_points: Number of wavelength points to consider for sparam convergence
            plot: Plot sparam convergence results and save png

        Returns:
            Convergence data in pd.DataFrame. S-params are absolute value squared (i.e. |S11|^2)
            | thresholds | S11   | S12   | ...
            | float      | float | float | ...
        """
        port_modes = port_modes or {}
        s = self.session
        cs = self.convergence_settings

        # Save original sim settings
        orig_mesh_accuracy = s.getnamed("FDTD", "mesh accuracy")
        s.setnamed("FDTD", "mesh accuracy", mesh_accuracy)

        s.select("FDTD::ports")
        orig_wavl_points = s.get("monitor frequency points")
        s.set("monitor frequency points", wavl_points)

        converged = False
        efield_intensity_threshold = 1e-1
        thresholds = []
        sparams = {}
        while not converged:
            self.convergence_settings.port_field_intensity_threshold = (
                efield_intensity_threshold
            )
            thresholds.append(efield_intensity_threshold)
            self.update_port_convergence(
                port_modes=port_modes, mesh_accuracy=mesh_accuracy, verbose=plot
            )
            sp = self.write_sparameters(overwrite=True)
            sp_data = sp.to_dict(orient="list")
            for k, v in sp_data.items():
                if not k == "wavelength":
                    if k not in sparams:
                        sparams[k] = [abs(np.array(v)) ** 2]
                    else:
                        sparams[k].append(abs(np.array(v)) ** 2)

            # Check whether sparams have converged.
            # Compare previous two iterations of sparams with current iteration of sparams and check whether the
            # difference is below the sparam_diff threshold
            if len(thresholds) > 2:
                sparam_diff = []
                for v in sparams.values():
                    # This accounts for multi wavelength convergence check
                    sparam_diff.append(max(abs(abs(v[-1]) ** 2 - abs(v[-2]) ** 2)))
                    sparam_diff.append(max(abs(abs(v[-1]) ** 2 - abs(v[-3]) ** 2)))
                if max(sparam_diff) < cs.sparam_diff:
                    converged = True
                    self.convergence_settings.port_field_intensity_threshold = (
                        efield_intensity_threshold
                    )
                    break

            efield_intensity_threshold = efield_intensity_threshold / 10

        sparams["thresholds"] = thresholds

        # Save convergence results
        df = pd.DataFrame(sparams)
        df.to_csv(
            str(
                self.dirpath / f"{self.component.name}_efield_intensity_convergence.csv"
            )
        )

        # Restore simulation settings
        s.setnamed("FDTD", "mesh accuracy", orig_mesh_accuracy)
        s.select("FDTD::ports")
        s.set("monitor frequency points", orig_wavl_points)

        if plot:
            for k, v in sparams.items():
                if not k == "threshold":
                    plt.figure()
                    plt.plot(sparams["thresholds"], v)
                    plt.xscale("log")
                    plt.xlabel("E-Field Intensity Threshold")
                    plt.ylabel("Magnitude")
                    plt.title(f"|{k}|^2")
                    plt.grid("on")
                    plt.tight_layout()
                    plt.savefig(
                        str(
                            self.dirpath
                            / f"{self.component.name}_efield_intensity_convergence_{k}.png"
                        )
                    )

        return df


if __name__ == "__main__":
    # import lumapi
    #
    # s = lumapi.FDTD()
    # component = gf.components.mmi1x2()
    # material_name_to_lumerical = dict(
    #     si="Si (Silicon) - Palik",
    #     substrate="Si (Silicon) - Palik",
    #     box="SiO2 (Glass) - Palik",
    #     clad="SiO2 (Glass) - Palik",
    # )  # or dict(si=3.45+2j)
    #
    # r = write_sparameters_lumerical(
    #     component=component,
    #     material_name_to_lumerical=material_name_to_lumerical,
    #     run=False,
    #     session=s,
    # )
    main()