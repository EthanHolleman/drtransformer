#
# drtransformer.landscape
# 
# Home of the TrafoLandscape object.
#

import logging
drlog = logging.getLogger(__name__)

import math
import numpy as np
from datetime import datetime

import RNA

from .rnafolding import (get_guide_graph, 
                         neighborhood_flooding,
                         find_fraying_neighbors,
                         top_down_coarse_graining)
from .linalg import mx_simulate, get_p8_detbal

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ #
# Transformer Landscape Object                                                 #
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ #
class TrafoLandscape:
    """ Implemented for unimolecular reactions.

    The backbone of any ribolands landscape object, inspired by networkx
    interface, although networkx dependency has been removed.

    Suggested attributes for nodes: structure, identity, energy, occupancy, active
    Suggested attributes for edges: weight
    """
    def __init__(self, sequence, vrna_md, prefix = ''):
        self.sequence = sequence
        self.md = vrna_md
        self.fc = RNA.fold_compound(sequence, vrna_md)
        _ = self.fc.mfe() # fill matrices

        self.prefix = prefix  # for autogenerated node IDs
        self.nodeID = 0       # for autogenerated node IDs

        # Private instance variables:
        self._nodes = dict()
        self._edges = dict()
        self._cg_edges = dict()

        # Default parameters:
        self.k0 = 2e5 # set directly
        self.minh = 0 # [dcal/mol] set using t_fast
        self.fpwm = 0 # set directly
        self.mfree = 6 # set directly
        self.transcript_length = 0 # set directly, updated automatically

    @property
    def RT(self):
        RT = 0.61632077549999997
        if self.md.temperature != 37.0:
            kelvin = 273.15 + self.md.temperature
            RT = (RT / 310.15) * kelvin
        return RT

    @property
    def transcript(self):
        return self.sequence[0:self.transcript_length]

    @property
    def nodes(self):
        return self._nodes

    @property
    def edges(self):
        return self._edges

    @property
    def cg_edges(self):
        return self._cg_edges

    def has_node(self, n):
        return n in self._nodes

    def has_edge(self, s1, s2):
        return (s1, s2) in self._edges

    def has_cg_edge(self, s1, s2):
        return (s1, s2) in self._cg_edges 

    def addnode(self, key, 
                structure = None, 
                occupancy = 0, 
                identity = None, 
                energy = None, 
                active = True, 
                pruned = 0, 
                lminreps = None,
                hiddennodes = None, 
                occtransfer = None):
        """ Add a node with specific tags:

        - Active (bool) is to filter a subset of interesting nodes.
        - Lminreps (set) is to return a set of nodes that are the local minimum
            reperesentatives of this node.
        - Hiddennodes (set) is to return a set of nodes that are associated with
            this local minimum.
        After coarse graining, a node should have either lminreps or
        hiddennodes set, but not both. 
        """
        assert key not in self._nodes
        if energy is not None:
            assert isinstance(energy, int), "Energy must be specified as integer."
        elif structure is not None:
            energy = int(round(self.fc.eval_structure(structure)*100))
        if identity is None:
            identity = f'{self.prefix}{self.nodeID}'
            self.nodeID += 1
        assert isinstance(identity, str)
        self._nodes[key] = {'structure': structure,
                            'occupancy': occupancy,
                            'identity': identity,
                            'energy': energy,
                            'active': active,
                            'pruned': pruned,
                            'lminreps': lminreps,
                            'hiddennodes': hiddennodes,
                            'occtransfer': occtransfer}
        return

    def addedge(self, n1, n2, weight = None, **kwargs):
        assert n1 in self._nodes
        assert n2 in self._nodes
        if (n1, n2) not in self._edges:
            self._edges[(n1, n2)] = {'weight': weight}
        self._edges[(n1, n2)].update(kwargs)
        return

    def sorted_nodes(self, attribute = 'energy', rev = False, nodes = None):
        """ Provide active nodes or new nodes, etc. if needed. """
        if nodes is None:
            nodes = self.nodes
        return sorted(nodes, key = lambda x: self.nodes[x][attribute], reverse = rev)

    def get_rate(self, s1, s2):
        """ Returns the direct transition rate of two secondary structures. """
        return self._edges[(s1, s2)]['weight'] if self.has_edge(s1, s2) else 0

    def get_saddle(self, s1, s2):
        """ Returns the saddle energy of a transition edge.  """
        return self.edges[(s1, s2)]['saddle_energy'] if self.has_edge(s1, s2) else None

    def get_cg_saddle(self, s1, s2):
        """ Returns the saddle energy of a coarse grained transition edge.  """
        return self.cg_edges[(s1, s2)]['saddle_energy'] if self.has_cg_edge(s1, s2) else None

    @property
    def local_mins(self):
        return (n for n in self.nodes if not self.nodes[n]['lminreps'])

    @property
    def active_local_mins(self):
        return (n for n in self.nodes if self.nodes[n]['active'] and not self.nodes[n]['lminreps'])

    @property
    def hidden_nodes(self):
        return (n for n in self.nodes if self.nodes[n]['lminreps'])

    @property
    def active_nodes(self):
        return (n for n in self.nodes if self.nodes[n]['active'] is True)

    @property
    def inactive_nodes(self):
        return (n for n in self.nodes if self.nodes[n]['active'] is False)

    def __repr__(self):
        return f"{self.__class__.__name__}({self.sequence}, {len(self.nodes)=}, {len(self.edges)=})"

    # ================ #
    # Algorithmic part #
    # ================ #

    def expand(self, performance_report = False):
        """ Find new secondary structures and determine their neighborhood in the landscape.

        The function adds to types of new structures: 
            1) The mfe structure for the current sequence length.
            2) The helix-fraying of all currently active structures.

        Returns:
            int: Number of new nodes.
            int: Number of cached old nodes.
        """
        fseq = self.sequence
        self.transcript_length += 1 
        if self.transcript_length > len(fseq):
            self.transcript_length = len(fseq)
        seq = self.transcript
        fc = self.fc
        mfree = self.mfree
        minh = self.minh

        # Calculate MFE of current transcript.
        mfess, _ = fc.backtrack(len(seq))
        future = '.' * (len(fseq) - len(seq))
        mfess = mfess + future

        i_time = datetime.now()

        # If there is no node because we are in the beginning, add the node.
        if len(self.nodes) == 0: 
            self.addnode(mfess, structure = mfess, occupancy = 1)
            nn = set([mfess])
            on = set()
            pr = (0, 0, 0, 0) if performance_report else None
        else: 
            md = self.md
            fpwm = self.fpwm
            parents = [x[0:len(seq)] for x in self.active_local_mins]
            fraying_nodes = find_fraying_neighbors(seq, md, parents, mfree = mfree)

            # 1) Add all new structures to the set of nodes.
            nn, on = set(), set()
            if mfess not in self.nodes:
                self.addnode(mfess, structure = mfess)
                nn.add(mfess)
                assert self.nodes[mfess]['active']
            elif not self.nodes[mfess]['active']:
                on.add(mfess)
                self.nodes[mfess]['active'] = True

            for fns in fraying_nodes.values():
                for fn in fns:
                    fn += future
                    if fn not in self.nodes:
                        self.addnode(fn, structure = fn)
                        nn.add(fn)
                        assert self.nodes[fn]['active']
                    elif not self.nodes[fn]['active']:
                        on.add(fn)
                        self.nodes[fn]['active'] = True

            f_time = datetime.now()

            ndata = {n[0:len(seq)]: d for n, d in self.nodes.items() if d['active']} 
            gnodes, gedges = get_guide_graph(seq, md, ndata.keys())
            assert all(ss != '' for (ss, en) in gnodes)

            for (ss, en) in gnodes:
                if ss + future not in self.nodes:
                    self.addnode(ss + future, structure = ss+future)
                    nn.add(ss + future)
                elif not self.nodes[ss + future]['active']:
                    on.add(ss + future)
                    self.nodes[ss + future]['active'] = True
            ndata = {n: d for n, d in self.nodes.items() if d['active']} 

            lgedges = set()
            for (x, y) in gedges:
                lgedges.add((x+future, y+future))
            gedges = lgedges

            g_time = datetime.now()

            # 2) Include edge-data from previous network if nodes are active.
            edata = {k: v for k, v in self.edges.items() if (self.nodes[k[0]]['active'] and 
                                                             self.nodes[k[1]]['active']) and 
                                                             v['saddle_energy'] is not None}

            ndata, edata = neighborhood_flooding((fseq, md, fpwm), ndata, gedges, tedges = edata, minh = minh)

            # 3) Extract new node data.
            for node in ndata:
                if node not in self.nodes:
                    self.addnode(node, structure = node, energy = ndata[node]['energy'])
                    nn.add(node)
                elif not self.nodes[node]['active']:
                    on.add(node)
                    self.nodes[node]['active'] = True
                assert self.nodes[node]['energy'] == ndata[node]['energy']

            # 4) Update to new edges.
            for (x, y) in edata:
                se = edata[(x, y)]['saddle_energy']
                self.addedge(x, y, saddle_energy = se)

            l_time = datetime.now()
            frayytime = (f_time - i_time).total_seconds() 
            guidetime = (g_time - f_time).total_seconds()
            floodtime = (l_time - g_time).total_seconds()
            tottime = (l_time - i_time).total_seconds()
            drlog.debug(f'{len(seq)=}, {tottime=}, {frayytime=}, {guidetime=}, {floodtime=}.')
            pr = (tottime, frayytime, guidetime, floodtime) if performance_report else None
        return nn, on, pr

    def get_coarse_network(self):
        """ Produce a smaller graph of local minima and best connections.

        It is useful to distinguish active vs inactive nodes here because new
        nodes get new IDs for simulation. As structures disappear and reappear,
        we keep a cache of inactive nodes for some time before finally deleting
        them.  
        """
        minh = self.minh

        ndata = dict()
        for n in self.nodes: 
            self.nodes[n]['lminreps'] = set()
            self.nodes[n]['hiddennodes'] = set()
            # Because this node remained inactive during graph expansion, we
            # can now safely transfer its occupancy.
            if not self.nodes[n]['active'] and self.nodes[n]['occupancy'] != 0:
                for tn in self.nodes[n]['occtransfer']:
                    assert self.nodes[tn]['active']
                    self.nodes[tn]['occupancy'] += self.nodes[n]['occupancy']/len(self.nodes[n]['occtransfer'])
                self.nodes[n]['occupancy'] = 0
        ndata = {n: d for n, d in self.nodes.items() if d['active']} # only active.
        edata = {k: v for k, v in self.edges.items() if self.nodes[k[0]]['active'] and self.nodes[k[1]]['active']}
        cg_ndata, cg_edata, cg_mapping = top_down_coarse_graining(ndata, edata, minh)
        assert all((n in ndata) for n in cg_ndata)

        # Translate coarse grain results to TL.
        self._cg_edges = dict()
        for (x, y) in cg_edata:
            se = cg_edata[(x, y)]['saddle_energy']
            ex = cg_ndata[x]['energy']
            bar = (se-ex) / 100
            self._cg_edges[(x, y)] = {'saddle_energy': se,
                                      'weight': self.k0 * math.e**(-bar/self.RT)}

        for lmin, hidden in cg_mapping.items():
            assert self.nodes[lmin]['active']
            for hn in hidden:
                assert self.nodes[hn]['active']
                self.nodes[hn]['lminreps'].add(lmin)
            self.nodes[lmin]['hiddennodes'] = hidden

        # Move occupancy to lmins.
        for hn in self.hidden_nodes:
            if not self.nodes[n]['active']:
                assert self.nodes[n]['occupancy'] == 0
            if self.nodes[hn]['occupancy']:
                for lrep in self.nodes[hn]['lminreps']:
                    self.nodes[lrep]['occupancy'] += self.nodes[hn]['occupancy']/len(self.nodes[hn]['lminreps'])
            self.nodes[hn]['occupancy'] = 0
            # NOTE: the following line could be optional! 
            # if hn remain active, that means they will be 
            # used as parents in the next round ...
            self.nodes[hn]['active'] = False
        return len(cg_ndata), len(cg_edata)

    def get_occupancies(self):
        snodes = sorted(self.active_local_mins, key = lambda x: self.nodes[x]['energy'])
        p0 = [self.nodes[n]['occupancy'] for n in snodes]
        #assert np.isclose(sum(p0), 1)
        return snodes, p0
        
    def get_equilibrium_occupancies(self, snodes):
        dim = len(snodes)
        R = np.zeros((dim, dim))
        for i, ni in enumerate(snodes):
            for j, nj in enumerate(snodes):
                if self.has_cg_edge(ni, nj):
                    R[j][i] = self.cg_edges[(ni, nj)]['weight']
        return get_p8_detbal(R)
 
    def set_occupancies(self, snodes, pt):
        for i, n in enumerate(snodes):
            self.nodes[n]['occupancy'] = pt[i]

    def simulate(self, snodes, p0, times, force = None, atol = 1e-4, rtol = 1e-4):
        assert np.isclose(sum(p0), 1)
        dim = len(snodes)

        if dim == 1:
            if force is None:
                force = [times[-1]]
            elif force[-1] < times[-1]:
                force.append(times[-1])
            if times[0] != force[0]:
                yield times[0], [1]
            for ft in force:
                yield ft, [1]
            return

        R = np.zeros((dim, dim))
        for i, ni in enumerate(snodes):
            for j, nj in enumerate(snodes):
                if self.has_cg_edge(ni, nj):
                    R[j][i] = self.cg_edges[(ni, nj)]['weight']

        for t, pt in mx_simulate(R, p0, times, force = force, atol = atol, rtol = rtol):
            yield t, pt
        return

    def prune(self, pmin, delth = 10, keep = None):
        """ TODO make sure return values make sense... distinguish lmins and hn.
        """
        new_inactive_lms = []
        tot_pruned = 0
        for lm in sorted(self.active_local_mins, key = lambda x: self.nodes[x]['occupancy']):
            if lm in keep:
                continue
            self.nodes[lm]['pruned'] = 0
            if tot_pruned + self.nodes[lm]['occupancy'] > pmin:
                break
            tot_pruned += self.nodes[lm]['occupancy']
            for hn in self.nodes[lm]['hiddennodes']:
                assert self.nodes[hn]['occupancy'] == 0
                self.nodes[hn]['active'] = False
            self.nodes[lm]['active'] = False
            new_inactive_lms.append(lm)

        def get_active_nbrs(lm, forbidden = None):
            # TODO: lazy and probably inefficient implementation.
            if forbidden is None:
                forbidden = set()
            forbidden.add(lm)
            found = False
            remaining = []
            for (x, y) in self.cg_edges:
                assert x != y
                if x == lm and y not in forbidden:
                    if self.nodes[y]['active']:
                        found = True
                        yield y
                    else:
                        remaining.append(y)
            if not found:
                for r in remaining:
                    assert self.nodes[r]['active'] is False
                    for a in get_active_nbrs(r, forbidden):
                        yield a
            return

        for lm in new_inactive_lms:
            assert not self.nodes[lm]['active']
            self.nodes[lm]['occtransfer'] = set(get_active_nbrs(lm))
            assert len(self.nodes[lm]['occtransfer']) > 0

        pn = set()
        dn = set()
        for node in list(self.nodes):
            if self.nodes[node]['active']:
                self.nodes[node]['pruned'] = 0
            else:
                self.nodes[node]['pruned'] += 1
                if self.nodes[node]['pruned'] == 1:
                    # this includes hidden nodes that were not part of the simulation
                    pn.add(node)
            drlog.debug(f'After pruning: {node} {self.nodes[node]}')
            if self.nodes[node]['pruned'] > delth:
                #TODO: probably quite inefficient...
                for (x, y) in set(self.edges):
                    if node in (x, y):
                        del self._edges[(x,y)]
                del self._nodes[node]
                dn.add(node)
        return pn, dn

