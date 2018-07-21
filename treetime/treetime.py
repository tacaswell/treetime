from __future__ import print_function, division, absolute_import
import numpy as np
from scipy import optimize as sciopt
from Bio import Phylo
from treetime import config as ttconf
from .utils import tree_layout
from .clock_tree import ClockTree

rerooting_mechanisms = ["min_dev", "best", "least-squares", "chisq"]

class TreeTime(ClockTree):
    """
    TreeTime is a wrapper class to ClockTree that adds additional functionality
    such as reroot, detection and exclusion of outliers, resolution of polytomies
    using temporal information, and relaxed molecular clock models
    """

    def __init__(self, *args,**kwargs):
        """
        TreeTime constructor

        Parameters
        -----------
         *args
            Arguments to construct ClockTree

         **kwargs
            Keyword arguments to construct the GTR model

        """
        super(TreeTime, self).__init__(*args, **kwargs)


    def run(self, root=None, infer_gtr=True, relaxed_clock=None, n_iqd = None,
            resolve_polytomies=True, max_iter=0, Tc=None, fixed_clock_rate=None,
            time_marginal=False, sequence_marginal=False, branch_length_mode='auto', **kwargs):

        """
        Run TreeTime reconstruction. Based on the input parameters, it divides
        the analysis into semi-independent jobs and conquers them one-by-one,
        gradually optimizing the tree given the temporal constarints and leaf
        node sequences.

        Parameters
        ----------

         root : str
            Try to find better root position on a given tree. If string is passed,
            the root will be searched according to the specified method. If none,
            use tree as-is.

            See :py:meth:`treetime.TreeTime.reroot` for available rooting methods.

         infer_gtr : bool
            If True, infer GTR model

         relaxed_clock : dic
            If not None, use autocorrelated molecular clock model. Specify the
            clock parameters as :code:`{slack:<slack>, coupling:<coupling>}` dictionary.

         n_iqd : int
            If not None, filter tree nodes which do not obey the molecular clock
            for the particular tree. The nodes, which deviate more than
            :code:`n_iqd` interquantile intervals from the molecular clock
            regression will be marked as 'BAD' and not used in the TreeTime
            analysis

         resolve_polytomies : bool
            If True, attempt to resolve multiple mergers

         max_iter : int
            Maximum number of iterations to optimize the tree

         Tc : float, str
            If not None, use coalescent model to correct the branch lengths by
            introducing merger costs.

            If Tc is float, it is interpreted as the coalescence time scale

            If Tc is str, it should be one of (:code:`opt`, :code:`skyline`)

         fixed_clock_rate : float
            Fixed clock rate to be used. If None, infer clock rate from the molecular clock.

         time_marginal : bool
            If True, perform a final round of marginal reconstruction of the node's positions.

         branch_length_mode : str
            Should be one of: :code:`joint`, :code:`marginal`, :code:`input`.

            If 'input', rely on the branch lengths in the input tree and skip directly
            to the maximum-likelihood ancestral sequence reconstruction.
            Otherwise, perform preliminary sequence reconstruction using parsimony
            algorithm and do branch length optimization

         **kwargs
            Keyword arguments needed by the dowstream functions


        """

        if (self.tree is None) or (self.aln is None and self.seq_len is None):
            self.logger("TreeTime.run: ERROR, alignment or tree are missing", 0)
            return ttconf.ERROR
        if (self.aln is None):
            branch_length_mode='input'

        if branch_length_mode not in ['joint', 'marginal', 'input']:
            branch_length_mode = self._set_branch_length_mode(branch_length_mode)

        # determine how to reconstruct and sample sequences
        seq_kwargs = {"marginal_sequences":sequence_marginal or (branch_length_mode=='marginal'),
                      "branch_length_mode":branch_length_mode,
                      "sample_from_profile":"root"}
        seq_LH = 0
        if "fixed_pi" in kwargs:
            seq_kwargs["fixed_pi"] = kwargs["fixed_pi"]
        if "do_marginal" in kwargs:
            time_marginal=kwargs["do_marginal"]

        # initially, infer ancestral sequences and infer gtr model if desired
        if branch_length_mode=='input':
            if self.aln:
                self.infer_ancestral_sequences(infer_gtr=infer_gtr, **seq_kwargs)
                self.prune_short_branches()
        else:
            self.optimize_sequences_and_branch_length(infer_gtr=infer_gtr,
                                                  max_iter=1, prune_short=True, **seq_kwargs)
        avg_root_to_tip = np.mean([x.dist2root for x in self.tree.get_terminals()])

        # optionally reroot the tree either by oldest, best regression or with a specific leaf
        if n_iqd or root=='clock_filter':
            if "plot_rtt" in kwargs and kwargs["plot_rtt"]:
                plot_rtt=True
            else:
                plot_rtt=False
            self.clock_filter(reroot='least-squares' if root=='clock_filter' else root,
                              n_iqd=n_iqd, plot=plot_rtt)
        elif root is not None:
            if self.reroot(root='least-squares')==ttconf.ERROR:
                return ttconf.ERROR


        if branch_length_mode=='input':
            if self.aln:
                self.infer_ancestral_sequences(**seq_kwargs)
        else:
            self.optimize_sequences_and_branch_length(max_iter=1, prune_short=False,
                                                      **seq_kwargs)

        # infer time tree and optionally resolve polytomies
        self.logger("###TreeTime.run: INITIAL ROUND",0)
        self.make_time_tree(clock_rate=fixed_clock_rate, time_marginal=False,
                            branch_length_mode=branch_length_mode,**kwargs)

        if self.aln:
            seq_LH = self.tree.sequence_marginal_LH if seq_kwargs['marginal_sequences'] else self.tree.sequence_joint_LH
        self.LH =[[seq_LH, self.tree.positional_joint_LH, 0]]

        if root is not None:
            if self.reroot(root='least-squares' if root=='clock_filter' else root)==ttconf.ERROR:
                return ttconf.ERROR

        # iteratively reconstruct ancestral sequences and re-infer
        # time tree to ensure convergence.
        niter = 0
        ndiff = 0
        while niter < max_iter:

            self.logger("###TreeTime.run: ITERATION %d out of %d iterations"%(niter+1,max_iter),0)
            # add coalescent prior
            if Tc and (Tc is not None):
                from .merger_models import Coalescent
                self.logger('TreeTime.run: adding coalescent prior with Tc='+str(Tc),1)
                self.merger_model = Coalescent(self.tree, Tc=avg_root_to_tip,
                                               date2dist=self.date2dist, logger=self.logger)

                if Tc=='skyline' and niter==max_iter-1: # restrict skyline model optimization to last iteration
                    self.merger_model.optimize_skyline(**kwargs)
                    self.logger("optimized a skyline ", 2)
                else:
                    if Tc in ['opt', 'skyline']:
                        self.merger_model.optimize_Tc()
                        self.logger("optimized Tc to %f"%self.merger_model.Tc.y[0], 2)
                    else:
                        try:
                            self.merger_model.set_Tc(Tc)
                        except:
                            self.logger("setting of coalescent time scale failed", 1, warn=True)

                self.merger_model.attach_to_tree()

            # estimate a relaxed molecular clock
            if relaxed_clock:
                self.relaxed_clock(**relaxed_clock)

            n_resolved=0
            if resolve_polytomies:
                # if polytomies are found, rerun the entire procedure
                n_resolved = self.resolve_polytomies()
                if n_resolved:
                    self.prepare_tree()
                    # when using the input branch length, only infer ancestral sequences
                    if branch_length_mode=='input':
                        if self.aln:
                            self.infer_ancestral_sequences(**seq_kwargs)
                    else: # otherwise reoptimize branch length while preserving branches without mutations
                        self.optimize_sequences_and_branch_length(prune_short=False,
                                                                  max_iter=0, **seq_kwargs)

                    self.make_time_tree(clock_rate=fixed_clock_rate, time_marginal=False,
                                        branch_length_mode=branch_length_mode, **kwargs)
                    if self.aln:
                        ndiff = self.infer_ancestral_sequences('ml',**seq_kwargs)
                else:
                    if self.aln:
                        ndiff = self.infer_ancestral_sequences('ml',**seq_kwargs)
                    self.make_time_tree(clock_rate=fixed_clock_rate, time_marginal=False,
                                        branch_length_mode=branch_length_mode,**kwargs)
            elif (Tc and (Tc is not None)) or relaxed_clock: # need new timetree first
                self.make_time_tree(clock_rate=fixed_clock_rate, time_marginal=False,
                                    branch_length_mode=branch_length_mode,**kwargs)
                if self.aln:
                    ndiff = self.infer_ancestral_sequences('ml',**seq_kwargs)
            else: # no refinements, just iterate
                if self.aln:
                    ndiff = self.infer_ancestral_sequences('ml',**seq_kwargs)
                self.make_time_tree(clock_rate=fixed_clock_rate, time_marginal=False,
                                    branch_length_mode=branch_length_mode,**kwargs)

            self.tree.coalescent_joint_LH = self.merger_model.total_LH() if Tc else 0.0

            if self.aln:
                seq_LH = self.tree.sequence_marginal_LH if seq_kwargs['marginal_sequences'] else self.tree.sequence_joint_LH
            self.LH.append([seq_LH, self.tree.positional_joint_LH, self.tree.coalescent_joint_LH])
            niter+=1

            if ndiff==0 & n_resolved==0:
                self.logger("###TreeTime.run: CONVERGED",0)
                break


        # if marginal reconstruction requested, make one more round with marginal=True
        # this will set marginal_pos_LH, which to be used as error bar estimations
        if time_marginal:
            self.logger("###TreeTime.run: FINAL ROUND - confidence estimation via marginal reconstruction", 0)
            self.make_time_tree(clock_rate=fixed_clock_rate, time_marginal=time_marginal,
                                branch_length_mode=branch_length_mode,**kwargs)
        return ttconf.SUCCESS


    def _set_branch_length_mode(self, branch_length_mode):
        '''
        if branch_length mode is not explicitly set, set according to
        empirical branch length distribution in input tree

        Parameters
        ----------

         branch_length_mode : str, 'input', 'joint', 'marginal'
            if the maximal branch length in the tree is longer than 0.05, this will
            default to 'input'. Otherwise set to 'joint'
        '''
        bl_dis = [n.branch_length for n in self.tree.find_clades() if n.up]
        max_bl = np.max(bl_dis)
        if max_bl>0.1:
            bl_mode = 'input'
        else:
            bl_mode = 'joint'
        self.logger("TreeTime._set_branch_length_mode: maximum branch length is %1.3e, using branch length mode %s"%(max_bl, bl_mode),1)
        return bl_mode


    def clock_filter(self, reroot='best', n_iqd=None, plot=False):
        '''
        Labels outlier branches that don't seem to follow a molecular clock
        and excludes them from subsequent molecular clock estimation and
        the timetree propagation.

        Parameters
        ----------
         reroot : str
            Method to find the best root in the tree (see :py:meth:`treetime.TreeTime.reroot` for options)

         n_iqd : int
            Number of iqd intervals. The outlier nodes are those which do not fall
            into :math:`IQD\cdot n_iqd` interval (:math:`IQD` is the interval between
            75\ :sup:`th` and 25\ :sup:`th` percentiles)

            If None, the default (3) assumed

         plot : bool
            If True, plot the results

        '''
        if n_iqd is None:
            n_iqd = ttconf.NIQD

        terminals = self.tree.get_terminals()
        if reroot:
            self.reroot(root=reroot)
        else:
            Treg = self.setup_TreeRegression(covariation=False)
            self.clock_model = Treg.regression()

        clock_rate = self.clock_model['slope']
        icpt = self.clock_model['intercept']

        res = {}
        for node in terminals:
            if hasattr(node, 'numdate_given') and  (node.numdate_given is not None):
                res[node] = node.dist2root - clock_rate*np.mean(node.numdate_given) - icpt
        residuals = np.array(list(res.values()))
        iqd = np.percentile(residuals,75) - np.percentile(residuals,25)
        for node,r in res.items():
            if abs(r)>n_iqd*iqd and node.up.up is not None:
                self.logger('TreeTime.ClockFilter: marking %s as outlier, residual %f interquartile distances'%(node.name,r/iqd), 3)
                node.bad_branch=True
            else:
                node.bad_branch=False

        if plot:
            self.plot_root_to_tip()

        # redo root estimation after outlier removal
        if reroot:
            self.reroot(root=reroot)
        return ttconf.SUCCESS

    def plot_root_to_tip(self, add_internal=False, label=True, ax=None, **kwargs):
        """
        Plot root-to-tip regression

        Parameters
        ----------

         add_internal : bool
            If true, plot internal node positions

         label : bool
            If true, label the plots

         ax: matplotlib axes
            If not None, use the provided matplotlib axes to plot the results

         **kwargs:
            Keyword arguments to be passed to :py:meth:`matplotlib.pyplot.scatter` function

        """
        Treg = self.setup_TreeRegression()
        if self.clock_model:
            cf = self.clock_model['covariation'] is True
        else:
            cf = False
        Treg.clock_plot(n_sigma=2, add_internal=add_internal, ax=ax, confidence=cf, reg=self.clock_model)


    def reroot(self, root='best', force_positive=True):
        """
        Find best root and re-root the tree to the new root

        Parameters
        ----------

         root : str
            Which method should be used to find the best root. Available methods are:

            :code:`best`, `least-squares`, `chisq` - minimize squared residual or chisq of root-to-tip regression

            :code:`oldest` - choose the oldest node

            :code:`<node_name>` - reroot to the node with name :code:`<node_name>`

            :code:`[<node_name1>, <node_name2>, ...]` - reroot to the MRCA of these nodes

          force_positive : bool
            only consider positive rates when searching for the optimal root
        """
        self.logger("TreeTime.reroot: with method or node: %s"%root,1)
        for n in self.tree.find_clades():
            n.branch_length=n.mutation_length

        if root in rerooting_mechanisms:
            new_root = self.reroot_to_best_root(covariation=root in ["best", "chisq", "min_dev"],
                                                force_positive=force_positive and (root!='min_dev'))
        else:
            from Bio import Phylo
            if isinstance(root,Phylo.BaseTree.Clade):
                new_root = root
            elif isinstance(root, list):
                new_root = self.tree.common_ancestor(*root)
            elif root in self._leaves_lookup:
                new_root = self._leaves_lookup[root]
            elif root=='oldest':
                new_root = sorted([n for n in self.tree.get_terminals()
                                   if n.numdate_given is not None],
                                   key=lambda x:np.mean(x.numdate_given))[0]
            else:
                self.logger('TreeTime.reroot -- WARNING: unsupported rooting mechanisms or root not found',2,warn=True)
                return ttconf.ERROR

            #this forces a bifurcating root, as we want. Branch lengths will be reoptimized anyway.
            #(Without outgroup_branch_length, gives a trifurcating root, but this will mean
            #mutations may have to occur multiple times.)
            self.tree.root_with_outgroup(new_root, outgroup_branch_length=new_root.branch_length/2)
            Treg = self.setup_TreeRegression(covariation=True)
            self.clock_model = Treg.regression()

        if new_root == ttconf.ERROR:
            return ttconf.ERROR

        self.logger("TreeTime.reroot: Tree was re-rooted to node "
                    +('new_node' if new_root.name is None else new_root.name), 2)

        self.tree.root.branch_length = self.one_mutation
        self.tree.root.numdate_given = None
        # set root.gamma bc root doesn't have a branch_length_interpolator but gamma is needed
        if not hasattr(self.tree.root, 'gamma'):
            self.tree.root.gamma = 1.0
        self.prepare_tree()
        for n in self.tree.find_clades():
            n.mutation_length = n.branch_length

        Treg = self.setup_TreeRegression(covariation=True)
        self.clock_model['r_val'] = Treg.explained_variance()

        return ttconf.SUCCESS


    def resolve_polytomies(self, merge_compressed=False):
        """
        Resolve the polytomies on the tree.

        The function scans the tree, resolves polytomies if present,
        and re-optimizes the tree with new topology. Note that polytomies are only
        resolved if that would result in higher likelihood. Sometimes, stretching
        two or more branches that carry several mutations is less costly than
        an additional branch with zero mutations (long branches are not stiff,
        short branches are).

        Parameters
        ----------
         merge_compressed : bool
            If True, keep compressed branches as polytomies. If False,
            return a strictly binary tree.

        Returns
        --------
         poly_found : int
            The number of polytomies found

        """
        self.logger("TreeTime.resolve_polytomies: resolving multiple mergers...",1)
        poly_found=0

        for n in self.tree.find_clades():
            if len(n.clades) > 2:
                prior_n_clades = len(n.clades)
                self._poly(n, merge_compressed)
                poly_found+=prior_n_clades - len(n.clades)

        obsolete_nodes = [n for n in self.tree.find_clades() if len(n.clades)==1 and n.up is not None]
        for node in obsolete_nodes:
            self.logger('TreeTime.resolve_polytomies: remove obsolete node '+node.name,4)
            if node.up is not None:
                self.tree.collapse(node)

        if poly_found:
            self.logger('TreeTime.resolve_polytomies: introduces %d new nodes'%poly_found,3)
        else:
            self.logger('TreeTime.resolve_polytomies: No more polytomies to resolve',3)
        return poly_found


    def _poly(self, clade, merge_compressed):

        """
        Function to resolve polytomies for a given parent node. If the
        number of the direct decendants is less than three (not a polytomy), does
        nothing. Otherwise, for each pair of nodes, assess the possible LH increase
        which could be gained by merging the two nodes. The increase in the LH is
        basically the tradeoff between the gain of the LH due to the changing the
        branch lenghts towards the optimal values and the decrease due to the
        introduction of the new branch with zero optimal length.
        """

        from .branch_len_interpolator import BranchLenInterpolator

        zero_branch_slope = self.gtr.mu*self.seq_len

        def _c_gain(t, n1, n2, parent):
            """
            cost gain if nodes n1, n2 are joined and their parent is placed at time t
            cost gain = (LH loss now) - (LH loss when placed at time t)
            """
            cg2 = n2.branch_length_interpolator(parent.time_before_present - n2.time_before_present) - n2.branch_length_interpolator(t - n2.time_before_present)
            cg1 = n1.branch_length_interpolator(parent.time_before_present - n1.time_before_present) - n1.branch_length_interpolator(t - n1.time_before_present)
            cg_new = - zero_branch_slope * (parent.time_before_present - t) # loss in LH due to the new branch
            return -(cg2+cg1+cg_new)

        def cost_gain(n1, n2, parent):
            """
            cost gained if the two nodes would have been connected.
            """
            try:
                cg = sciopt.minimize_scalar(_c_gain,
                    bounds=[max(n1.time_before_present,n2.time_before_present), parent.time_before_present],
                    method='Bounded',args=(n1,n2, parent))
                return cg['x'], - cg['fun']
            except:
                self.logger("TreeTime._poly.cost_gain: optimization of gain failed", 3, warn=True)
                return parent.time_before_present, 0.0


        def merge_nodes(source_arr, isall=False):
            mergers = np.array([[cost_gain(n1,n2, clade) if i1<i2 else (0.0,-1.0)
                                    for i1,n1 in enumerate(source_arr)]
                                for i2, n2 in enumerate(source_arr)])
            LH = 0
            while len(source_arr) > 1 + int(isall):
                # max possible gains of the cost when connecting the nodes:
                # this is only a rough approximation because it assumes the new node positions
                # to be optimal
                new_positions = mergers[:,:,0]
                cost_gains = mergers[:,:,1]
                # set zero to large negative value and find optimal pair
                np.fill_diagonal(cost_gains, -1e11)
                idxs = np.unravel_index(cost_gains.argmax(),cost_gains.shape)
                if (idxs[0] == idxs[1]) or cost_gains.max()<0:
                    self.logger("TreeTime._poly.merge_nodes: node is not fully resolved "+clade.name,4)
                    return LH

                n1, n2 = source_arr[idxs[0]], source_arr[idxs[1]]
                LH += cost_gains[idxs]

                new_node = Phylo.BaseTree.Clade()

                # fix positions and branch lengths
                new_node.time_before_present = new_positions[idxs]
                new_node.branch_length = clade.time_before_present - new_node.time_before_present
                new_node.clades = [n1,n2]
                n1.branch_length = new_node.time_before_present - n1.time_before_present
                n2.branch_length = new_node.time_before_present - n2.time_before_present

                # set parameters for the new node
                new_node.up = clade
                n1.up = new_node
                n2.up = new_node
                new_node.cseq = clade.cseq
                self._store_compressed_sequence_to_node(new_node)

                new_node.mutations = []
                new_node.mutation_length = 0.0
                new_node.branch_length_interpolator = BranchLenInterpolator(new_node, self.gtr, one_mutation=self.one_mutation)
                clade.clades.remove(n1)
                clade.clades.remove(n2)
                clade.clades.append(new_node)
                self.logger('TreeTime._poly.merge_nodes: creating new node as child of '+clade.name,3)
                self.logger("TreeTime._poly.merge_nodes: Delta-LH = " + str(cost_gains[idxs].round(3)), 3)

                # and modify source_arr array for the next loop
                if len(source_arr)>2: # if more than 3 nodes in polytomy, replace row/column
                    for ii in np.sort(idxs)[::-1]:
                        tmp_ind = np.arange(mergers.shape[0])!=ii
                        mergers = mergers[tmp_ind].swapaxes(0,1)
                        mergers = mergers[tmp_ind].swapaxes(0,1)

                    source_arr.remove(n1)
                    source_arr.remove(n2)
                    new_gains = np.array([[cost_gain(n1,new_node, clade) for n1 in source_arr]])
                    mergers = np.vstack((mergers, new_gains)).swapaxes(0,1)

                    source_arr.append(new_node)
                    new_gains = np.array([[cost_gain(n1,new_node, clade) for n1 in source_arr]])
                    mergers = np.vstack((mergers, new_gains)).swapaxes(0,1)
                else: # otherwise just recalculate matrix
                    source_arr.remove(n1)
                    source_arr.remove(n2)
                    source_arr.append(new_node)
                    mergers = np.array([[cost_gain(n1,n2, clade) for n1 in source_arr]
                                       for n2 in source_arr])

            return LH

        stretched = [c for c  in clade.clades if c.mutation_length < c.clock_length]
        compressed = [c for c in clade.clades if c not in stretched]

        if len(stretched)==1 and merge_compressed is False:
            return 0.0

        LH = merge_nodes(stretched, isall=len(stretched)==len(clade.clades))
        if merge_compressed and len(compressed)>1:
            LH += merge_nodes(compressed, isall=len(compressed)==len(clade.clades))

        return LH


    def print_lh(self, joint=True):
        """
        Print the total likelihood of the tree given the constrained leaves

        Parameters
        ----------

         joint : bool
            If true, print joint LH, else print marginal LH

        """
        try:
            u_lh = self.tree.unconstrained_sequence_LH
            if joint:
                s_lh = self.tree.sequence_joint_LH
                t_lh = self.tree.positional_joint_LH
                c_lh = self.tree.coalescent_joint_LH
            else:
                s_lh = self.tree.sequence_marginal_LH
                t_lh = self.tree.positional_marginal_LH
                c_lh = 0

            print ("###  Tree Log-Likelihood  ###\n"
                " Sequence log-LH without constraints: \t%1.3f\n"
                " Sequence log-LH with constraints:    \t%1.3f\n"
                " TreeTime sequence log-LH:            \t%1.3f\n"
                " Coalescent log-LH:                   \t%1.3f\n"
               "#########################"%(u_lh, s_lh,t_lh, c_lh))
        except:
            print("ERROR. Did you run the corresponding inference (joint/marginal)?")


    def relaxed_clock(self, slack=None, coupling=None, **kwargs):
        """
        Allow the mutation rate to vary on the tree (relaxed molecular clock).
        Changes of the mutation rates from one branch to another are penalized.
        In addition, deviation of the mutation rate from the mean rate is
        penalized.

        Parameters
        ----------
         slack : float
            Maximum change in substitution rate between parent and child nodes

         coupling : float
            Maximum difference in substitution rates in sibling nodes

        """
        if slack is None: slack=ttconf.MU_ALPHA
        if coupling is None: coupling=ttconf.MU_BETA
        self.logger("TreeTime.relaxed_clock: slack=%f, coupling=%f"%(slack, coupling),2)

        c=1.0/self.one_mutation
        for node in self.tree.find_clades(order='postorder'):
            opt_len = node.mutation_length

            # opt_len \approx 1.0*len(node.mutations)/node.profile.shape[0] but calculated via gtr model
            # contact term: stiffness*(g*bl - bl_opt)^2 + slack(g-1)^2 =
            #               (slack+bl^2) g^2 - 2 (bl*bl_opt+1) g + C= k2 g^2 + k1 g + C
            node._k2 = slack + c*node.branch_length**2/(opt_len+self.one_mutation)
            node._k1 = -2*(c*node.branch_length*opt_len/(opt_len+self.one_mutation) + slack)
            # coupling term: \sum_c coupling*(g-g_c)^2 + Cost_c(g_c|g)
            # given g, g_c needs to be optimal-> 2*coupling*(g-g_c) = 2*child.k2 g_c  + child.k1
            # hence g_c = (coupling*g - 0.5*child.k1)/(coupling+child.k2)
            # substituting yields
            for child in node.clades:
                denom = coupling+child._k2
                node._k2 += coupling*(1.0-coupling/denom)**2 + child._k2*coupling**2/denom**2
                node._k1 += (coupling*(1.0-coupling/denom)*child._k1/denom \
                            - coupling*child._k1*child._k2/denom**2 \
                            + coupling*child._k1/denom)

        for node in self.tree.find_clades(order='preorder'):
            if node.up is None:
                node.gamma =- 0.5*node._k1/node._k2
            else:
                if node.up.up is None:
                    g_up = node.up.gamma
                else:
                    g_up = node.up.branch_length_interpolator.gamma
                node.branch_length_interpolator.gamma = (coupling*g_up - 0.5*node._k1)/(coupling+node._k2)

###############################################################################
### rerooting
###############################################################################

    def reroot_to_best_root(self, covariation=True, force_positive=True, **kwarks):
        '''
        Determine the node that, when the tree is rooted on this node, results
        in the best regression of temporal constraints and root to tip distances.

        Parameters
        ----------

         infer_gtr : bool
            If True, infer new GTR model after re-root

         covariation : bool
            account for covariation structure when rerooting the tree

         force_positive : bool
            only accept positive evolutionary rate estimates when rerooting the tree

        '''
        for n in self.tree.find_clades():
            n.branch_length=n.mutation_length
        self.logger("TreeTime.reroot_to_best_root: searching for the best root position...",2)
        Treg = self.setup_TreeRegression(covariation=covariation)
        self.clock_model = Treg.optimal_reroot(force_positive=force_positive)
        self.clock_model['covariation'] = covariation
        return self.clock_model['node']


def plot_vs_years(tt, years = 1, ax=None, confidence=None, ticks=True, **kwargs):
    '''
    Converts branch length to years and plots the time tree on a time axis.

    Parameters
    ----------
     tt : TreeTime object
        A TreeTime instance after a time tree is inferred

     years : int
        Width of shaded boxes indicating blocks of years

     ax : matplotlib axes
        Axes to be used to plot, will create new axis if None

     confidence : tuple, float
        Draw confidence intervals. This assumes that marginal time tree inference was run.
        Confidence intervals are either specified as an interval of the posterior distribution
        like (0.05, 0.95) or as the weight of the maximal posterior region , e.g. 0.9

     **kwargs : dict
        Key word arguments that are passed down to Phylo.draw

    '''
    import matplotlib.pyplot as plt
    tt.branch_length_to_years()
    if ax is None:
        fig = plt.figure()
        ax = plt.subplot(111)
    else:
        fig = None
    # draw tree
    if "label_func" not in kwargs:
        nleafs = tt.tree.count_terminals()
        kwargs["label_func"] = lambda x:x.name if (x.is_terminal() and nleafs<30) else ""
    Phylo.draw(tt.tree, axes=ax, **kwargs)

    # set axis labels
    offset = tt.tree.root.numdate - tt.tree.root.branch_length
    xticks = ax.get_xticks()
    dtick = xticks[1]-xticks[0]
    shift = offset - dtick*(offset//dtick)
    tick_vals = [x+offset-shift for x in xticks]
    ax.set_xticks(xticks - shift)
    ax.set_xticklabels(map(str, tick_vals))
    ax.set_xlabel('year')
    ax.set_ylabel('')
    ax.set_xlim((0,np.max([n.numdate for n in tt.tree.get_terminals()])+2-offset))

    # put shaded boxes to delineate years
    if years:
        ylim = ax.get_ylim()
        xlim = ax.get_xlim()
        if type(years) in [int, float]:
            dyear=years
        from matplotlib.patches import Rectangle
        for yi,year in enumerate(np.arange(np.floor(tick_vals[0]), tick_vals[-1],dyear)):
            pos = year - offset
            r = Rectangle((pos, ylim[1]-5),
                          dyear, ylim[0]-ylim[1]+10,
                          facecolor=[0.7+0.1*(1+yi%2)] * 3,
                          edgecolor=[1,1,1])
            ax.add_patch(r)
            if year in tick_vals and pos>xlim[0] and pos<xlim[1] and ticks:
                ax.text(pos,ylim[0]-0.04*(ylim[1]-ylim[0]),str(int(year)),
                        horizontalalignment='center')
        ax.set_axis_off()

    # add confidence intervals to the tree graph -- grey bars
    if confidence:
        tree_layout(tt.tree)
        if not hasattr(tt.tree.root, "marginal_inverse_cdf"):
            print("marginal time tree reconstruction required for confidence intervals")
            return ttconf.ERROR
        elif len(confidence)==2:
            cfunc = tt.get_confidence_interval
        elif len(confidence)==1:
            cfunc = tt.get_max_posterior_region
        else:
            print("confidence needs to be either a float (for max posterior region) or a two numbers specifying lower and upper bounds")
            return ttconf.ERROR

        for n in tt.tree.find_clades():
            pos = cfunc(n, confidence)
            ax.plot(pos-offset, np.ones(len(pos))*n.ypos, lw=3, c=(0.5,0.5,0.5))
    return fig, ax

def treetime_to_newick(tt, outf):
    Phylo.write(tt.tree, outf, 'newick')


if __name__=="__main__":
    pass


