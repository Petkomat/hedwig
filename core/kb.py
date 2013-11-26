'''
Knowledge-base class.

@author: anze.vavpetic@ijs.si
'''
from collections import defaultdict
from bitarray import bitarray
from rdflib import RDF, RDFS, URIRef

from example import Example
from predicate import UnaryPredicate
from helpers import avg, std
from settings import EXAMPLE_SCHEMA, HEDWIG


class ExperimentKB:
    '''
    The knowledge base for one specific experiment.
    '''
    def __init__(self, triplets, score_fun,
                 user_namespaces=[],
                 instances_as_leaves=True):
        '''
        Initialize the knowledge base with the given triplet graph.
        The target class is given with 'target_class' - this is the
        class to be described in the induction step.
        '''
        self.g = triplets
        self.user_namespaces = user_namespaces
        self.score_fun = score_fun
        self.sub_class_of = defaultdict(list)
        self.super_class_of = defaultdict(list)
        self.predicates = set()
        self.binary_predicates = set()
        self.class_values = set()

        # Parse the examples schema
        self.g.parse(EXAMPLE_SCHEMA, format='n3')

        # Extract the available examples from the graph
        ex_subjects = self.g.subjects(predicate=RDF.type, object=HEDWIG.Example)
        self.examples_uris = [ex for ex in ex_subjects]
        self.uri_to_idx = {}

        examples = []
        for i, ex_uri in enumerate(self.examples_uris):

            # Query for annotation link objects
            annot_objects = self.g.objects(subject=ex_uri,
                                           predicate=HEDWIG.annotated_with)

            annotation_links = [annot for annot in annot_objects]
            annotations = []
            weights = {}
            to_uni = lambda s: unicode(s).encode('ascii', 'ignore')

            for link in annotation_links:

                # Query for annotation objects via this link
                annot_objects = self.g.objects(subject=link,
                                               predicate=HEDWIG.annotation)
                annotation = [to_uni(one) for one in annot_objects][0]

                # Query for weights on this link
                weight_objects = self.g.objects(subject=link,
                                                predicate=HEDWIG.weight)
                weights_list = [one for one in weight_objects]

                if weights_list:
                    weights[annotation] = float(weights_list[0])

                annotations.append(annotation)

            # Scores
            score_list = list(self.g.objects(subject=ex_uri,
                                             predicate=HEDWIG.score))
            if score_list:
                score = float(score_list[0])
            else:
                # Classes
                score_list = list(self.g.objects(subject=ex_uri,
                                                 predicate=HEDWIG.class_label))
                score = str(score_list[0])
                self.class_values.add(score)

            self.uri_to_idx[ex_uri] = i
            examples.append(Example(i, str(ex_uri), score,
                                    annotations=annotations,
                                    weights=weights))

        self.examples = examples

        if not self.examples:
            raise Exception("No examples provided! Examples should be \
                             instances of %s." % HEDWIG)

        # Ranked or class-labeled data
        self.target_type = self.examples[0].target_type

        # Get the subClassOf hierarchy
        for sub, obj in self.g.subject_objects(predicate=RDFS.subClassOf):
            if self.user_defined(sub) and self.user_defined(obj):
                self.add_sub_class(sub, obj)

        # Include the instances as predicates as well
        if instances_as_leaves:
            for sub, obj in self.g.subject_objects(predicate=RDF.type):
                if self.user_defined(sub) and self.user_defined(obj):
                    self.add_sub_class(sub, obj)

        # Find the user-defined object predicates defined between examples
        examples_as_domain = set(self.g.subjects(object=HEDWIG.Example,
                                                 predicate=RDFS.domain))

        examples_as_range = set(self.g.subjects(object=HEDWIG.Example,
                                                predicate=RDFS.range))

        for pred in examples_as_domain.intersection(examples_as_range):
            if self.user_defined(pred):
                self.binary_predicates.add(str(pred))

        # Calculate the members for each predicate
        self.members = defaultdict(set)
        for ex in examples:
            for inst in ex.annotations:
                if instances_as_leaves:
                    self.members[inst].add(ex.id)
                else:
                    # Query for 'parents' of a given instance
                    inst_parents = self.g.objects(subject=URIRef(inst),
                                                  predicate=RDF.type)

                    for obj in inst_parents:
                        self.members[str(obj)].add(ex.id)

        # Find the root classes
        roots = filter(lambda pred: self.sub_class_of[pred] == [],
                       self.super_class_of.keys())

        # Add a dummy root
        self.dummy_root = 'root'
        self.predicates.add(self.dummy_root)
        for root in roots:
            self.add_sub_class(root, self.dummy_root)

        self.sub_class_of_closure = defaultdict(set)
        for pred in self.super_class_of.keys():
            self.sub_class_of_closure[pred].update(self.sub_class_of[pred])

        # Calc the closure to get the members of the subClassOf hierarchy
        def closure(pred, lvl):
            children = self.super_class_of[pred]
            self.levels[lvl].add(pred)

            if children:
                mems = set()
                for child in children:
                    parent_closure = self.sub_class_of_closure[pred]
                    self.sub_class_of_closure[child].update(parent_closure)
                    mems.update(closure(child, lvl + 1))
                self.members[pred] = mems

                return mems
            else:
                return self.members[pred]

        # Level-wise predicates
        self.levels = defaultdict(set)

        # Run the closure from root
        closure(self.dummy_root, 0)

        # Members of non-unary predicates
        self.binary_members = defaultdict(dict)
        self.reverse_binary_members = defaultdict(dict)

        for pred in self.binary_predicates:
            pairs = self.g.subject_objects(predicate=URIRef(pred))

            for pair in pairs:
                el1, el2 = self.uri_to_idx[pair[0]], self.uri_to_idx[pair[1]]
                if self.binary_members[pred].has_key(el1):
                    self.binary_members[pred][el1].append(el2)
                else:
                    self.binary_members[pred][el1] = [el2]

                # Add the reverse as well
                if self.reverse_binary_members[pred].has_key(el2):
                    self.reverse_binary_members[pred][el2].append(el1)
                else:
                    self.reverse_binary_members[pred][el2] = [el1]

        # Bitset of examples for input and output
        self.binary_domains = {}
        for pred in self.binary_predicates:
            self.binary_domains[pred] = (
                self.indices_to_bits(self.binary_members[pred].keys()),
                self.indices_to_bits(self.reverse_binary_members[pred].keys())
            )

        # Calc the corresponding bitsets
        self.bit_members = {}
        for pred in self.members.keys():
            self.bit_members[pred] = self.indices_to_bits(self.members[pred])

        self.bit_binary_members = defaultdict(dict)
        self.reverse_bit_binary_members = defaultdict(dict)

        for pred in self.binary_members.keys():

            for el in self.binary_members[pred].keys():
                indices = self.indices_to_bits(self.binary_members[pred][el])
                self.bit_binary_members[pred][el] = indices

            for el in self.reverse_binary_members[pred].keys():
                reverse_members = self.reverse_binary_members[pred][el]
                indices = self.indices_to_bits(reverse_members)
                self.reverse_bit_binary_members[pred][el] = indices

        # Statistics
        if self.target_type == Example.Ranked:
            self.mean = avg([ex.score for ex in self.examples])
            self.sd = std([ex.score for ex in self.examples])
        else:
            self.distribution = defaultdict(int)
            for ex in self.examples:
                self.distribution[ex.score] += 1

    def user_defined(self, uri):
        '''
        Is this resource user defined?
        '''
        defined = True
        if self.user_namespaces:
            defined = any([uri.startswith(ns) for ns in self.user_namespaces])

        return defined

    def add_sub_class(self, sub, obj):
        '''
        Adds the resource 'sub' as a subclass of 'obj'.
        '''
        to_uni = lambda s: unicode(s).encode('ascii', 'ignore')
        sub, obj = to_uni(sub), to_uni(obj)

        self.predicates.update([sub, obj])
        self.sub_class_of[sub].append(obj)
        self.super_class_of[obj].append(sub)

    def super_classes(self, pred):
        '''
        Returns all super classes of pred (with transitivity).
        '''
        return self.sub_class_of_closure[pred]

    def get_root(self):
        '''
        Root predicate, which covers all examples.
        '''
        return UnaryPredicate(self.dummy_root, self.get_full_domain(), self,
                              custom_var_name='X')

    def get_subclasses(self, predicate, producer_pred=None):
        '''
        Returns a list of subclasses (as predicate objects) for 'predicate'.
        '''
        return self.super_class_of[predicate.label]

    def get_members(self, predicate, bit=True):
        '''
        Returns the examples for this predicate,
        either as a bitset or a set of ids.
        '''
        members = None
        if predicate in self.predicates:
            if bit:
                members = self.bit_members[predicate]
            else:
                members = self.members[predicate]
        else:
            if bit:
                members = self.bit_binary_members[predicate]
            else:
                members = self.binary_members[predicate]

        return members

    def get_reverse_members(self, predicate, bit=True):
        '''
        Returns the examples for this predicate,
        either as a bitset or a set of ids.
        '''
        reverse_members = None
        if bit:
            reverse_members = self.reverse_bit_binary_members[predicate]
        else:
            reverse_members = self.reverse_binary_members[predicate]

        return reverse_members

    def get_domains(self, predicate):
        '''
        Returns the bitsets for input and outputexamples
        of the binary predicate 'predicate'.
        '''
        return self.binary_domains[predicate]

    def get_examples(self):
        '''
        Returns all examples for this experiment.
        '''
        return self.examples

    def n_examples(self):
        '''
        Returns the number of examples.
        '''
        return len(self.examples)

    def get_full_domain(self):
        '''
        Returns a bitset covering all examples.
        '''
        return bitarray([True] * self.n_examples())

    def get_empty_domain(self):
        '''
        Returns a bitset covering no examples.
        '''
        return bitarray([False] * self.n_examples())

    def get_score(self, ex_idx):
        '''
        Returns the score for example id 'ex_idx'.
        '''
        return self.examples[ex_idx].score

    def bits_to_indices(self, bits):
        '''
        Converts the bitset to a set of indices.
        '''
        return bits.search(bitarray([1]))

    def indices_to_bits(self, indices):
        '''
        Converts the indices to a bitset.
        '''
        bits = self.get_empty_domain()
        for idx in indices:
            bits[idx] = True
        return bits