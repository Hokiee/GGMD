import pandas as pd
import numpy as np
import argparse
import logging
import yaml
import time
from yaml import Loader

from optimizers.optimizer_factory import create_optimizer

from generative_models.FNL_JTNN.fast_jtnn.gen_latent import encode_smiles, decoder
from generative_models.AutoGrow.mutation.execute_mutations import Mutator
from generative_models.AutoGrow.crossover.execute_crossover import CrossoverOp

Log = logging.getLogger(__name__)

# Hack to keep molvs package from issuing debug message on bad Unicode string
molvs_log = logging.getLogger('molvs')
molvs_log.setLevel(logging.WARNING)



def create_generative_model(params):
    """
    Factory function for creating optmizer objects of the correct subclass for params.optmizer_type.
    Args:
        params: parameters to pass
    Returns:
        optmizer (object):  wrapper
    Raises: 
        ValueError: Only params.VAE_type = "JTNN" is supported
    """

    if params.model_type.lower() == 'jtnn-fnl':
        return JTNN_FNL(params)
    elif params.model_type.lower() == 'autogrow':
        return AutoGrow(params)
    else:
        raise ValueError("Unknown model_type %s" % params.model_type)



class GenerativeModel(object):
    def __init__(self, params, **kwargs):
        """
        Initialization method for the GenerativeModel class

        Args:
            params (namespace object): contains the parameters used to initialize the class
        MJT Note: do we need the **kwargs argument?
        """
        self.params = params
    
    def optimize(self):
        """
        optimize function not implemented in super class
        """
        raise NotImplementedError



class JTNN_FNL(GenerativeModel):

    def __init__(self, params):
        self.encoder = encode_smiles(params)
        self.decoder = decoder(params)
        self.is_first_epoch = True

        #New optimizer structure STB
        self.optimizer = create_optimizer(params)
        self.mate_prob = params.mate_prob
        self.max_population = params.max_population
        self.mutate_prob = params.mutate_prob
        self.mutation_std = params.mutation_std
        self.tree_sd = 4.86
        self.molg_sd = 0.0015

    def encode(self, smiles) -> list:
        print("Encoding ", len(smiles), " molecules")
        
        t1 = time.time()

        chromosome = self.encoder.encode(smiles)
        
        t2 = time.time()
        print("Encoding took ", t2-t1, " seconds")

        return list(chromosome)

    def decode(self, chromosome) -> list:
        print("Decoding ", len(chromosome), " molecules")

        t1 = time.time()
        
        # Parallel:
        smiles = self.decoder.decode_simple_2(chromosome)

        # Not parallel:
        #smiles = self.decoder.decode_simple(chromosome)

        t2 = time.time()

        print("Decoding took ", t2-t1, " seconds")

        return smiles
        
    def crossover(self, population):
        print("Crossover beginning population size: ", len(population))
        num_children = int(self.mate_prob * self.max_population) #STB int((0.3 * 100)) = 30
        parents_idx = np.random.randint(0, len(population), (num_children, 2)) #Sets up the indexes for parents in shape [[parent1, parent2], ...]
        
        parent_1 = []
        parent_2 = []
        child_chrom = []
        for i in range(num_children):
            parents = population.iloc[parents_idx[i]]
            parent_chrom = np.vstack(parents["chromosome"].values)
            parent_1.append(parents.compound_id.values[0])
            parent_2.append(parents.compound_id.values[1])

            selected_genes = np.random.randint(0, 2, self.chromosome_length)
            child_chromosome = np.where(selected_genes, parent_chrom[1], parent_chrom[0])
            child_chrom.append(child_chromosome)

        children_df = pd.DataFrame({"chromosome": child_chrom, "fitness": np.full(num_children, np.nan), "parent1_id": parent_1, "parent2_id": parent_2})
        
        population = pd.concat([population, children_df])
        population.reset_index(drop=True, inplace=True)
        print("Number of children: ", len(children_df), " length of total population: ", len(population))
        
        return population
    
    def mutate(self, population):
        #TODO: Need to test this method. My question is that we need to decide if mutations
        # should happen in place or if a mutated individual should create a new individual in 
        # the population. Mutations happen after crossover and can happen to new or old individuals

        mut_indices = np.where(np.random.rand(len(population)) < self.mutate_prob)[0]

        for idx in mut_indices:
            chromosome = population['chromosome'].iloc[idx]

            tree_vec, mol_vec = np.hsplit(chromosome, 2)

            tree_mut_ind = np.where(np.random.rand(len(tree_vec)) < self.mutate_prob)
            tree_vec[tree_mut_ind] = np.random.normal(loc=tree_vec[tree_mut_ind], scale=self.tree_sd * self.mutation_std)
            
            mol_mut_ind = np.where(np.random.rand(len(mol_vec)) < self.mutate_prob)
            mol_vec[mol_mut_ind] = np.random.normal(loc=mol_vec[mol_mut_ind], scale=self.molg_sd * self.mutation_std)

            
            num_pts_mutated = len(tree_mut_ind) + len(mol_mut_ind)

            if num_pts_mutated > 0:
                chromosome = np.concatenate([tree_vec, mol_vec])

                if np.isnan(population['fitness'].iloc[idx]) == False:
                    # If the individuals fitness is not nan, then this is an individual
                    # from a previous generation. We need to reset fitness, smiles, parent ids
                    # and compound_id.
                    population['parent1_id'].iloc[idx] = population['compound_id'].iloc[idx]
                    population['parent2_id'].iloc[idx] = np.nan
                    population['smiles'].iloc[idx] = np.nan
                    population['fitness'].iloc[idx] = np.nan
                    population['compound_id'].iloc[idx] = np.nan

                population['chromosome'].iloc[idx] = chromosome

        return population

    def sort(self, population):
        """
        This function splits the population into two sets: one set that contains the new individuals and one set that contains the unchanged individuals
        The new individuals need to be decoded, scored and added to the data tracker. The unchanged individuals have already been decoded, scored and tracked.
        This function sends the unchanged individuals to the genetic optimizer which stores the individuals until the next generation and returns the new
        individuals to be decoded, scored, and tracked
        Paramters:
            - population: dataframe of whole population
        Returns:
            - population_of_new_individuals: Pandas DataFrame of the individuals created in this generation
        """

        #Surviving individuals have real values for the fitness, smiles, and compound_id columns
        retained_population = population[population['fitness'].isna() == False]
        retained_population.drop(['parent1_id', 'parent2_id'], axis=1, inplace=True)
        self.optimizer.set_retained_population(retained_population)

        #Individuals that have been created this generation have a NaN associated with fitness, smiles, and compound_id columns
        population_of_new_individuals = population[population['compound_id'].isna()]
        return population_of_new_individuals

    def optimize(self, population):
        """
        This is the function responsible for handling all tasks related to optimization. For JTVAE, this includes encoding, 
        then sending the latent vectors (in the form of a pandas dataframe) to the genetic optimizer code. Once the population
        is returned, the latent vectors are decoded to SMILES strings. The resulting population is returned to the main loop 
        for scoring.

        Arguments:
        population - Pandas dataframe containing columns for id, smiles, and fitness

        Returns: 
        population - Pandas dataframe containing new smiles strings, ids, and 
        """
        
        if self.is_first_epoch:
            #Encode:
            
            smiles = population['smiles']
            chromosome = self.encode(smiles)

            
            assert len(smiles) == len(chromosome)

            population['chromosome'] = chromosome

            self.chromosome_length = len(population["chromosome"].iloc[0]) #When I set self.population, this can be moved potentially

            self.is_first_epoch = False

        #Optimize:
        print("Optimizing")

        population = self.optimizer.optimize(population)
        population = self.crossover(population=population)
        populaiton = self.mutate(population)

        print("sort")
        population = self.sort(population)

        #Decode:
        smiles = self.decode(population['chromosome'].tolist())

        population['smiles'] = smiles
        
        return population

def test_decoder(args):
    print("Running FNL's JTNN Test function: ")
    fname = '/mnt/projects/ATOM/blackst/GMD/LOGP-JTVAE-PAPER/Raw-Data/ZINC/all.txt'
    # Encoding time for  249456  molecules:  511.4566116333008  seconds
    # 
    #with open(args.smiles_input_file) as f:
    with open(fname) as f:
        smiles_list = [line.strip("\r\n ").split()[0] for line in f]

    smiles_original = smiles_list[:50]
    print("There are ", len(smiles_original), " molecules in the smiles list")

    model = create_generative_model(args)

    t0 = time.time()
    chromosome = model.encode(smiles_original)
    t1 = time.time()
    print("Encoding time for ", len(smiles_original), " molecules: ", (t1-t0), " seconds")
    smiles = model.decode(chromosome)
    t2 = time.time()
    print("Decoding time for ", len(smiles_original), " molecules: ", (t2-t1), " seconds")

    counter = 0
    for i in range(len(smiles_original)):
        if smiles_original[i] != smiles[i]:
            counter += 1

    print("Number of smiles incorrectly decoded: ", counter, " Reconstruction error: ", 100*(counter / (len(smiles_original))), "%")



class AutoGrow(GenerativeModel):
    def __init__(self, params):
        self.num_crossovers = params.num_crossovers
        self.num_mutations = params.num_mutations
        self.num_elite = params.num_elite

        self.optimizer = create_optimizer(params)
        self.mutator = Mutator(params)
        self.CrossoverOp = CrossoverOp(params)

    def mutate(self, smiles_list, new_smiles_list):
        
        new_smiles_list = self.mutator.make_mutants(generation_num=1, num_mutants_to_make=self.num_mutations, ligands_list=smiles_list, new_mutation_smiles_list=new_smiles_list)
        return new_smiles_list
    
    def crossover(self, population):
        
        print("enter crossover function")
        new_smiles_list = self.CrossoverOp.make_crossovers(generation_num=1, num_crossovers_to_make=self.num_crossovers, list_previous_gen_smiles=population['smiles'].tolist(), new_crossover_smiles_list=[])
        print("done crossover: ", len(new_smiles_list), " new smiles")
        return new_smiles_list
    
    def optimize(self, population):
        print(f"starting population size: {population.shape}")
        print(population.head())

        #generate mutation_pop
        new_smiles_list = []
        mut_pop_full = False
        while mut_pop_full == False:

            mutation_pop = self.optimizer.select_non_elite(population, self.num_mutations)
            #print(f"Mutation population: size {mutation_pop.shape}")
            
            #mutate mutation_pop
            new_smiles_list.extend(self.mutate(mutation_pop['smiles'].tolist(), new_smiles_list))
            
            #filter mutation_pop 

            if len(mutation_pop) == self.num_mutations:
                mut_pop_full = True

        #generate crossover_pop
        cross_pop_full = False
        while cross_pop_full == False:

            crossover_pop = self.optimizer.select_non_elite(population, self.num_crossovers)
            #print(f"Crossover population: size {crossover_pop.shape}")
            #print(crossover_pop.head())

            #crossover crossover_pop
            new_smiles_list.append(self.crossover(crossover_pop))
            #filter crossover_pop

            if len(crossover_pop) == self.num_crossovers:
                cross_pop_full = True

        #generate elite_pop
        elite_pop_full = False
        while elite_pop_full == False:

            elite_pop = self.optimizer.select_non_elite(population, self.num_elite)
            print(f"Elite population: size {elite_pop.shape}")
            print(elite_pop.head())

            #filter elite_pop

            if len(elite_pop) == self.num_elite:
                elite_pop_full = True
        
        #combine mutation_pop, crossover_pop, elite_pop
        combined_population = pd.concat([mutation_pop, crossover_pop, elite_pop])
        print("combined shape: ", combined_population.shape)
        print(combined_population.head())

        #Return combined_population
        return combined_population



def test_autogrow(args):
    model = create_generative_model(args)

    with open(args.smiles_input_file) as f:
        smiles_list = [line.strip("\r\n ").split()[0] for line in f]

    smiles = smiles_list[:50]

    model.optimize(smiles)

class CHAR_VAE(GenerativeModel):

    def __init__(self, params):
        pass
    def encode(self):
        pass
    def decode(self):
        pass
    def optimize(self):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-config', help="Config file location *.yml", action='append', default="/mnt/projects/ATOM/blackst/FNLGMD/examples/LogP_JTVAE/config.yaml")
    args = parser.parse_args()

    #for conf_fname in args.config:
    #    with open(conf_fname, 'r') as f:
    #        parser.set_defaults(**yaml.load(f, Loader=Loader))
    with open("/mnt/projects/ATOM/blackst/FNLGMD/source/config.yaml", 'r') as f:
        parser.set_defaults(**yaml.load(f, Loader=Loader))

    args = parser.parse_args()

    if args.model_type == 'jtnn-fnl':
        test_decoder(args)
    elif args.model_type == 'autogrow':
        test_autogrow(args)
