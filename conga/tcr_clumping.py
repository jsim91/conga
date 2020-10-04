# import scanpy as sc
# import random
import pandas as pd
from os.path import exists
from collections import OrderedDict#, Counter
# from sklearn.metrics import pairwise_distances
# from sklearn.utils import sparsefuncs
# from sklearn.decomposition import KernelPCA
import numpy as np
# import scipy
# from scipy.cluster import hierarchy
# from scipy.spatial.distance import squareform, cdist
# from scipy.sparse import issparse#, csr_matrix
from scipy.stats import poisson
# from anndata import AnnData
import sys
# import os
from sys import exit
from . import util
from . import preprocess
from .tcrdist import tcr_sampler


def assess_tcr_clumping(
        adata,
        num_random_samples,
        outfile_prefix,
        radii = [24, 48, 72, 96],
        pseudocount = 0.25,
        pvalue_threshold = 1.0,
):
    if not util.tcrdist_cpp_available():
        print('conga.tcr_clumping.assess_tcr_clumping:: need to compile the C++ tcrdist executables')
        exit(1)

    organism = adata.uns['organism']
    num_clones = adata.shape[0]

    radii = [int(x+0.1) for x in radii] #ensure integers

    outprefix = outfile_prefix+'_tcr_clumping'

    tcrs = preprocess.retrieve_tcrs_from_adata(adata)
    junctions_df = tcr_sampler.parse_tcr_junctions(adata.uns['organism'], tcrs)

    achains_bg = tcr_sampler.resample_shuffled_tcr_chains(num_random_samples, 'A', junctions_df)
    bchains_bg = tcr_sampler.resample_shuffled_tcr_chains(num_random_samples, 'B', junctions_df)

    achains_file = outprefix+'_bg_achains.tsv'
    bchains_file = outprefix+'_bg_bchains.tsv'
    tcrs_file = outprefix+'_tcrs.tsv'

    pd.DataFrame({'va':[x[0] for x in achains_bg], 'cdr3a':[x[2] for x in achains_bg]})\
      .to_csv(achains_file, sep='\t', index=False)

    pd.DataFrame({'vb':[x[0] for x in bchains_bg], 'cdr3b':[x[2] for x in bchains_bg]})\
      .to_csv(bchains_file, sep='\t', index=False)

    adata.obs['va cdr3a vb cdr3b'.split()].to_csv(tcrs_file, sep='\t', index=False)


    # find neighbors in fg tcrs up to max(radii) #######################################
    exe = util.path_to_tcrdist_cpp_bin+'find_neighbors'
    agroups, bgroups = preprocess.setup_tcr_groups(adata)
    agroups_filename = outprefix+'_agroups.txt'
    bgroups_filename = outprefix+'_bgroups.txt'
    np.savetxt(agroups_filename, agroups, fmt='%d')
    np.savetxt(bgroups_filename, bgroups, fmt='%d')

    db_filename = '{}tcrdist_info_{}.txt'.format(util.path_to_tcrdist_cpp_db, organism)

    tcrdist_threshold = max(radii)
    cmd = '{} -f {} -t {} -d {} -o {} -a {} -b {}'\
          .format(exe, tcrs_file, tcrdist_threshold, db_filename, outprefix, agroups_filename, bgroups_filename)
    util.run_command(cmd, verbose=True)

    nbr_indices_filename = '{}_nbr{}_indices.txt'.format(outprefix, tcrdist_threshold)
    nbr_distances_filename = '{}_nbr{}_distances.txt'.format(outprefix, tcrdist_threshold)

    if not exists(nbr_indices_filename) or not exists(nbr_distances_filename):
        print('find_neighbors failed:', exists(nbr_indices_filename), exists(nbr_distances_filename))
        exit(1)

    all_nbrs = []
    all_distances = []
    for line1, line2 in zip(open(nbr_indices_filename,'r'), open(nbr_distances_filename,'r')):
        l1 = line1.split()
        l2 = line2.split()
        assert len(l1) == len(l2)
        #ii = len(all_nbrs)
        all_nbrs.append([int(x) for x in l1])
        all_distances.append([int(x) for x in l2])
    assert len(all_nbrs) == num_clones

    # compute distributions vs background chains
    exe = util.path_to_tcrdist_cpp_bin + 'calc_distributions'
    outfile = outprefix+'_dists.txt'
    cmd = '{} -f {} -m {} -d {} -a {} -b {} -o {}'\
          .format(exe, tcrs_file, max(radii), db_filename, achains_file, bchains_file, outfile)
    util.run_command(cmd, verbose=True)

    if not exists(outfile):
        print('calc_distributions failed: missing', outfile)
        exit(1)

    counts = np.loadtxt(outfile, dtype=int)
    counts = np.cumsum(counts, axis=1)
    assert counts.shape == (num_clones, max(radii)+1)
    n_bg_pairs = num_random_samples * num_random_samples
    freqs = np.maximum(pseudocount, counts.astype(float))/n_bg_pairs

    clone_sizes = adata.obs['clone_sizes']

    # use poisson to find nbrhoods with more tcrs than expected; have to handle agroups/bgroups
    dfl = []

    is_clumped = np.full((num_clones,), False)

    for ii in range(num_clones):
        ii_freqs = freqs[ii]
        ii_dists = all_distances[ii]
        for radius in radii:
            num_nbrs = np.sum(x<=radius for x in ii_dists)
            max_nbrs = np.sum( (agroups!=agroups[ii]) & (bgroups!=bgroups[ii]))
            if num_nbrs:
                # adjust for number of tests
                mu = max_nbrs * ii_freqs[radius]
                pval = len(radii) * num_clones * poisson.sf( num_nbrs-1, mu )
                if pval< pvalue_threshold:
                    is_clumped[ii] = True
                    print('nbrs: {:2d} {:9.6f} radius: {:2d} pval: {:9.1e} {:6d} tcr: {:3d} {} {}'\
                          .format( num_nbrs, mu, radius, pval, counts[ii][radius], clone_sizes[ii],
                                   ' '.join(tcrs[ii][0][:3]), ' '.join(tcrs[ii][1][:3])))
                    dfl.append( OrderedDict(clone_index=ii,
                                            nbr_radius=radius,
                                            pvalue_adj=pval,
                                            num_nbrs=num_nbrs,
                                            expected_num_nbrs=mu,
                                            raw_count=counts[ii][radius],
                                            va   =tcrs[ii][0][0],
                                            ja   =tcrs[ii][0][1],
                                            cdr3a=tcrs[ii][0][2],
                                            vb   =tcrs[ii][1][0],
                                            jb   =tcrs[ii][1][1],
                                            cdr3b=tcrs[ii][1][2],
                    ))
    results_df = pd.DataFrame(dfl)
    if results_df.shape[0] == 0:
        return results_df

    # identify groups of related hits?
    all_clumped_nbrs = {}
    for l in results_df.itertuples():
        ii = l.clone_index
        radius = l.nbr_radius
        clumped_nbrs = set(x for x,y in zip(all_nbrs[ii], all_distances[ii]) if y<= radius and is_clumped[x])
        clumped_nbrs.add(ii)
        if ii in all_clumped_nbrs:
            all_clumped_nbrs[ii] = all_clumped_nbrs[ii] | clumped_nbrs
        else:
            all_clumped_nbrs[ii] = clumped_nbrs


    clumped_inds = sorted(all_clumped_nbrs.keys())
    assert len(clumped_inds) == np.sum(is_clumped)

    # make nbrs symmetric
    for ii in clumped_inds:
        for nbr in all_clumped_nbrs[ii]:
            assert nbr in all_clumped_nbrs
            all_clumped_nbrs[nbr].add(ii)

    all_smallest_nbr = {}
    for ii in clumped_inds:
        all_smallest_nbr[ii] = min(all_clumped_nbrs[ii])

    while True:
        updated = False
        for ii in clumped_inds:
            nbr = all_smallest_nbr[ii]
            new_nbr = min(nbr, np.min([all_smallest_nbr[x] for x in all_clumped_nbrs[ii]]))
            if nbr != new_nbr:
                all_smallest_nbr[ii] = new_nbr
                updated = True
        if not updated:
            break
    # define clusters, choose cluster centers
    clusters = np.array([0]*num_clones) # 0 if not clumped

    cluster_number=0
    for ii in clumped_inds:
        nbr = all_smallest_nbr[ii]
        if ii==nbr:
            cluster_number += 1
            members = [ x for x,y in all_smallest_nbr.items() if y==nbr]
            clusters[members] = cluster_number

    for ii, nbrs in all_clumped_nbrs.items():
        for nbr in nbrs:
            assert clusters[ii] == clusters[nbr] # confirm single-linkage clusters

    assert not np.any(clusters[is_clumped]==0)
    assert np.all(clusters[~is_clumped]==0)

    results_df['cluster'] = [ clusters[x.clone_index] for x in results_df.itertuples()]


    return results_df
