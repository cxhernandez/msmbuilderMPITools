#!/usr/bin/env python
"""
Assign clusters to a protein-ligand system with RMSD using separate sets of atoms for the
alignment and distance computation.

This script uses hybrid MPI/OpenMP paralleism in addition to highly optimized
SIMD vectorization within the compute kernels. Using multiple MPI processes
requires running this command using your MPI implementation's process manager, 
e.g. `mpirun`, `mpiexec`, or `aprun`. The number of OpenMP threads can be
controled by setting the OMP_NUM_THREADS environment variable. (e.g.
$ export OMP_NUM_THREADS=4; mpirun -np 16 clusterLP-MPI <options>)

Authors: Carlos Xavier Hernández
Contributers: Robert McGibbon
"""

from __future__ import print_function
import h5py, glob, optparse, os, sys, time, datetime, itertools, warnings
if sys.version_info < (2, 7):
    print("Your Python interpreter is too old. Please consider upgrading.")
    sys.exit(1)
import numpy as np
try:
    import mdtraj as md
    import mdtraj.io
    import mdtraj.rmsd as RMSD
except ImportError:
    print("This package requires the latest development version of MDTraj")
    print("which can be downloaded from https://github.com/rmcgibbo/mdtraj")
    sys.exit(1)
try:
    from mpi4py import MPI
except:
    print("This package requires mpi4py, which can be downloaded")
    print("from https://pypi.python.org/pypi/mpi4py")
    sys.exit(1)

#-----------------------------------
# Globals
#-----------------------------------
COMM = MPI.COMM_WORLD
RANK = COMM.rank
SIZE = COMM.size

#-----------------------------------
# Code
#-----------------------------------

def printM(message, *args):
    if RANK == 0:
        if len(args) == 0:
            print(message)
        else:
            print(message % args)
            
class timing(object):
    "Context manager for printing performance"
    def __init__(self, name):
        self.name = name
    def __enter__(self):
        self.start = time.time()
    def __exit__(self, ty, val, tb):
        end = time.time()
        print("<RANK %d> PERFORMANCE [%s] : %0.3f seconds" % (RANK, self.name, end-self.start))
        return False

def parse_cmdln():
    parser = optparse.OptionParser()
    parser.add_option('-d', '--dir', dest='dir', type='string', help='Directory containing trajectories')
    parser.add_option('-e', '--ext', dest='ext', type='string', help='File extension', default='dcd')
    parser.add_option('-t', '--top', dest='topology', type='string', help='Topology File')
    parser.add_option('-g', '--gens', dest='gens', type='string', help='Gens file', default="./Gens.lh5")
    parser.add_option('-pi', '--protein_indices', dest='protein_indices', type='string', help='List of protein indices')
    parser.add_option('-li', '--ligand_indices', dest='ligand_indices', type='string', help='List of ligand indices')
    (options, args) = parser.parse_args()
        
    return (options, args)
    
def assign(trajectory, gens, pi, li):
    rmsds = np.zeros((gens.n_frames, trajectory.n_frames))
    for i, gen in enumerate(gens):
        rmsds[i, :] = md.RMSD(trajectory, gen, atom_indices = li, precentered=True)
    return rmsds.argmin(axis = 0), rmsds.min(axis = 0)

def main(gens, trajectories, idx, li, pi, topology, directory, ext):
    
    top = topology.topology.copy()
    topology.restrict_atoms(idx)
    gens.superpose(topology, atom_indices=pi)
    n_traj = len(trajectories)
    
    with timing('Assigning...'):
        
        assignments = []
        distances = []
        n_frames = []
        for trajectory in trajectories:
            traj = md.load(trajectory, top = top, atom_indices = idx)
            assignment, distance = assign(traj.superpose(topology, atom_indices = pi), gens)
            assignments.append(assignment)
            distances.append(distance)
            n_frames.append(traj.n_frames)
            
    COMM.Barrier()
    all_assignments, all_distances, all_n_frames, all_n_traj, all_trajectory_files = COMM.gather(assignments, root = 0), COMM.gather(distances, root = 0), COMM.gather(n_frames, root = 0), COMM.gather(n_traj, root = 0), COMM.gather(trajectories, root = 0)
    
    if RANK == 0:
        
        n_trajectories, max_frames = np.sum(all_n_traj), np.max(np.max(all_n_frames))
        A = -1*np.ones((n_trajectories, max_frames))
        AD = -1*np.ones((n_trajectories, max_frames))
        all_trajectory_files = list(itertools.chain.from_iterable(all_trajectory_files))
        order = [all_trajectory_files.index(i) for i in glob.glob(directory + "/*." + ext)]
        all_assignments = list(itertools.chain.from_iterable(all_assignments))
        all_distances = list(itertools.chain.from_iterable(all_distances))
        
        with timing('Writing assignments...'):
            for j, i in enumerate(order):
                    A[j, :len(all_assignments[i])-1] = all_assignments[i]
                    AD[j, :len(all_distances[i])-1] = all_distances[i]
                    
            os.makedirs('Data/', exist_ok = True)        
            md.io.saveh('Data/Assignments.h5', A, completed_trajs = np.ones(n_trajectories, dtype=bool))
            md.io.saveh('Data/Assignments.h5.distances', AD, completed_trajs = np.ones(n_trajectories, dtype=bool))
            
        printM('Done!')
                
    
if __name__ == "__main__":
    
    (options, args) = parse_cmdln()
    trajectories = glob.glob(options['dir'] + "/*." + options['ext'])
    
    if RANK == 0:
        
        try:
            if not options.dir:
                parser.error('Please supply a directory.')
            if not options.topology:
                parser.error('Please supply a topology file.')
        except SystemExit:
            if SIZE > 1:
                COMM.Abort()
            exit()
        
        trajectories = [trajectories[i::SIZE] for i in range(SIZE)]
        
    else:
        trajectories = None
    
    topology = mdtraj.load(options['topology'])
    
    if options['protein_indices'] and options['ligand_indices']:
        
        pi, li = np.loadtxt(options['protein_indices']), np.loadtxt(options['ligand_indices'])
        idx = np.union1d(pi, li)
        pi, li = range(len(pi)), li = range(len(pi), len(pi)+len(li))
        
    else:
        idx = range(topology.n_atoms)
        
    gens = md.load(options['gens'], top=topology.topology, atom_indices=idx)
    
    trajectories = COMM.scatter(trajectories, root=0)
    
    printM('Starting...')
    
    main(gens, trajectories, idx, li, pi, topology, options['dir'], options['ext'])