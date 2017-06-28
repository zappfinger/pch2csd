import os
from copy import deepcopy
from glob import glob
from io import StringIO
from typing import List, Dict, Tuple

from pch2csd.patch import Module, Patch, CableColor, Cable
from pch2csd.resources import get_template_module_path, get_template_path, get_template_dir
from pch2csd.util import LogMixin, preprocess_csd_code


class UdoTemplate(LogMixin):
    def __init__(self, mod: Module):
        self.mod_type = mod.type
        self.mod_type_name = mod.type_name
        self.logger_set_name('UdoTemplate')
        with open(get_template_module_path(mod.type)) as f:
            self.lines = [l.strip() for l in f]
        self.udo_lines, self.args, self.maps = self._parse_headers()

    def __repr__(self):
        return f'UdoTemplate({self.mod_type_name}, {self.mod_type}.txt)'

    def __str__(self):
        return self.__repr__()

    def _parse_headers(self):
        udo_lines = []
        args = []  # List[List[str]]
        maps = []  # List[List[str]]

        for i, l in enumerate(self.lines):
            if l.startswith(';@ args'):
                udo_lines.append(i)
                a = [s.strip() for s in l.replace(';@ args', '').split(',')]
                args.append(a)
            elif l.startswith(';@ map'):
                m = [s.strip() for s in l.replace(';@ map', '').strip().split(' ')]
                maps.append(m)
        if self._validate_headers(udo_lines, args, maps):
            return udo_lines, args, maps
        else:
            return [], [], []

    def _validate_headers(self, udo_lines, args, maps):
        valid = True
        if len(args) == 0:
            self.log.error("%s: no opcode 'args' annotations were found in the template", self.__repr__())
            valid = False
        else:
            for i, a in enumerate(args):
                if len(a) != 3:
                    self.log.error("%s:%d the 'args' annotation should have exactly three arguments",
                                   self.__repr__(), udo_lines[i])
                    valid = False
            if len(args[0][0]) != len(maps):
                self.log.error("%s: the number of 'map' annotations should be equal to the number of module parameters",
                               self.__repr__())
                # TODO: validate map annotations
        return valid


class Udo(LogMixin):
    def __init__(self, patch: Patch, mod: Module):
        self.patch = patch
        self.mod = mod
        self.tpl = UdoTemplate(mod)
        self.udo_variant = self._choose_udo_variant()
        _, self.in_types, self.out_types = self.header
        self.inlets, self.outlets = self._init_zak_connections()

    def __repr__(self):
        return self.get_name()

    @property
    def header(self):
        if len(self.tpl.args) == 0:
            self.log.error(f"Can't create a UDO because {self.tpl} wasn't parsed properly (see log for details).")
            raise ValueError
        return self.tpl.args[self.udo_variant]

    def get_name(self) -> str:
        if len(self.tpl.args) < 2:
            return self.mod.type_name
        else:
            return f'{self.mod.type_name}_v{self.udo_variant}'

    def get_src(self) -> str:
        if len(self.tpl.args) < 2:
            return '\n'.join(self.tpl.lines[self.tpl.udo_lines[0] + 1:])
        offset = self.tpl.udo_lines[self.udo_variant] + 1
        udo_src = []
        for l in self.tpl.lines[offset:]:
            udo_src.append(l)
            if l.strip().startswith('endop'):
                break
        udo_src[0] = udo_src[0].replace(self.mod.type_name, self.get_name())
        return '\n'.join(udo_src)

    def _choose_udo_variant(self) -> int:
        v = 0
        cables = self.patch.find_all_incoming_cables(self.mod.location, self.mod.id)
        if len(self.tpl.args) < 2 or cables is None:
            return v
        cs_rates = {c.jack_to: CableColor.to_cs_rate_char(c.color) for i, c in enumerate(cables)}
        tpl_v0_ins = self.tpl.args[0][1]
        tpl_v1_ins = self.tpl.args[1][1]
        for i, r in cs_rates.items():
            if tpl_v0_ins[i] != r and tpl_v1_ins[i] == r:
                v = 1
                break
        return v

    def get_params(self) -> List[float]:
        tpl_param_def = self.tpl.args[self.udo_variant][0]
        params = self.patch.find_mod_params(self.mod.location, self.mod.id)
        if len(tpl_param_def) != len(params.values):
            self.log.error(f"Template '{self.tpl}' has different number of parameters "
                           f"than it was found in the parsed module '{self.mod}'. "
                           "Returning -1s for now.")
            return [-1] * params.num_params
        # TODO mappings
        return [self._map_value(i, v, params.values) for i, v in enumerate(params.values)]

    def get_statement(self):
        s = StringIO()
        s.write(self.get_name())
        s.write('(')
        s.write('/* Params */ ')
        s.write(', '.join([str(f) for f in self.get_params()]))
        s.write(', /* Inlets */ ')
        s.write(', '.join([str(i) for i in self.inlets]))
        s.write(', /* Outlets */ ')
        s.write(', '.join([str(i) for i in self.outlets]))
        s.write(')')
        return s.getvalue()

    def _init_zak_connections(self):
        ins, outs = self.in_types, self.out_types
        return [ZakSpace.trash_bus] * len(ins), [ZakSpace.zero_bus] * len(outs)

    def _map_value(self, i, v, all_vals):
        m = self.tpl.maps[i]
        if m[0] == 'd':
            table = self.patch.data.value_maps[m[1]]
        elif m[0] == 's':
            dependent_val = all_vals[int(m[1]) - 1]
            table = self.patch.data.value_maps[m[dependent_val + 2]]
        else:
            self.log.error(f'Mapping type {m[0]} is not supported')
            raise ValueError
        return table[v]


class ZakSpace:
    zero_bus = 0
    trash_bus = 1

    def __init__(self):
        self.aloc_i = 2
        self.kloc_i = 2
        self.zakk: Dict[Tuple[int, int], int] = {}  # Tuple[mod_id, inlet_id] -> zak_loc
        self.zaka: Dict[Tuple[int, int], int] = {}  # Tuple[mod_id, inlet_id] -> zak_loc

    def _init_zak(self):
        self.aloc_i = 2
        self.kloc_i = 2
        self.zakk: Dict[Tuple[int, int], int] = {}  # Tuple[mod_id, inlet_id] -> zak_loc
        self.zaka: Dict[Tuple[int, int], int] = {}  # Tuple[mod_id, inlet_id] -> zak_loc

    def connect_patch(self, p: Patch) -> List[Udo]:
        self._init_zak()
        udos: Dict[int, Udo] = deepcopy({m.id: Udo(p, m) for m in p.modules})
        for c in p.cables:
            mf, jf, mt, jt = c.module_from, c.jack_from, c.module_to, c.jack_to
            if udos[mf].out_types[jf] == udos[mt].in_types[jt]:
                self._zak_connect_direct(c, udos)
            else:
                raise NotImplementedError('Patch cord type conversion is not implemented yet.')
        return list(udos.values())

    def _aloc_new(self) -> int:
        self.aloc_i += 1
        return self.aloc_i

    def _kloc_new(self) -> int:
        self.kloc_i += 1
        return self.kloc_i

    def _zak_connect_direct(self, c: Cable, udos: Dict[int, Udo]):
        mf, jf, mt, jt = c.module_from, c.jack_from, c.module_to, c.jack_to
        rate_type = udos[mf].out_types[jf]
        if rate_type not in 'ka':
            raise ValueError(f'Unknown rate type: {rate_type}')
        out_id = (mf, jf)
        zak = self.zakk if rate_type == 'k' else self.zaka
        if out_id not in zak:
            zak[out_id] = self._kloc_new() if rate_type == 'k' else self._aloc_new()
        zak_id = zak[out_id]
        udos[mf].outlets[jf] = zak_id
        udos[mt].inlets[jt] = zak_id


class Csd:
    def __init__(self, p: Patch, zak: ZakSpace, udos: List[Udo]):
        self.patch = p
        self.zak = zak
        self.udos = udos

    def get_code(self) -> str:
        return '\n'.join([self.header,
                          self.zakinit,
                          self.ft_statements,
                          self.udo_defs,
                          self.instr_va,
                          self.instr_fx,
                          self.footer])

    @property
    def header(self) -> str:
        path = get_template_path('csd_header')
        with open(path, 'r') as f:
            return preprocess_csd_code(f.read()) + '\n'

    @property
    def footer(self) -> str:
        path = get_template_path('csd_footer')
        with open(path, 'r') as f:
            return preprocess_csd_code(f.read()) + '\n'

    @property
    def ft_statements(self) -> str:
        s = StringIO()
        s.write('\n')
        path_wildcard = os.path.join(get_template_dir(), 'csd_ft_*.txt')
        for ft in glob(path_wildcard):
            with open(ft, 'r') as f:
                s.write(preprocess_csd_code(f.read()))
                s.write('\n')
        return s.getvalue()

    @property
    def zakinit(self) -> str:
        return f'zakinit {self.zak.aloc_i}, {self.zak.kloc_i}'

    @property
    def instr_va(self) -> str:
        s = StringIO()
        s.write('instr 1 ; Voice area\n')
        s.write('\n'.join([udo.get_statement() for udo in self.udos]))
        s.write('\nendin\n')
        return s.getvalue()

    @property
    def instr_fx(self):
        s = StringIO()
        s.write('instr 2 ; FX area\n')
        s.write('; TODO')
        s.write('\nendin\n')
        return s.getvalue()

    @property
    def udo_defs(self):
        udo_src = {u.get_name(): u.get_src() for u in self.udos}
        names = sorted(udo_src.keys())
        return '\n\n'.join([preprocess_csd_code(udo_src[n]) for n in names]) + '\n'