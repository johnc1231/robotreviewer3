"""
the BiasRobot class takes the full text of a clinical trial as
input as a string, and returns bias information as a dict which
can be easily converted to JSON.

    text = "Streptomycin Treatment of Pulmonary Tuberculosis: A Medical Research Council Investigation..."

    robot = BiasRobot()
    annotations = robot.annotate(text)

Implements the models which were validated in the paper:

Marshall IJ, Kuiper J, & Wallace BC. RobotReviewer: evaluation of a system for automatically assessing bias in clinical trials. Journal of the American Medical Informatics Association 2015.doi:10.1093/jamia/ocv044
"""

# Authors:  Iain Marshall <mail@ijmarshall.com>
#           Joel Kuiper <me@joelkuiper.com>
#           Byron Wallce <byron.wallace@utexas.edu>

import json
import uuid
import os
import operator
import pickle
from collections import OrderedDict, defaultdict

import robotreviewer
from robotreviewer.textprocessing import tokenizer
from robotreviewer.ml.classifier import MiniClassifier
from robotreviewer.ml.vectorizer import ModularVectorizer

import sys
sys.path.append('robotreviewer/ml') # need this for loading the rationale_CNN module
from rationale_CNN import RationaleCNN, Document

import numpy as np
import re

import keras
import keras.backend as K
from keras.models import load_model


class BiasRobot:

    def __init__(self, top_k=3):
        """
        `top_k` refers to 'top-k recall'.

        top-1 recall will return the single most relevant sentence
        in the document, and top-3 recall the 3 most relevant.

        The validation study assessed the accuracy of top-3 and top-1
        and we suggest top-3 as default

        """
        self.bias_domains = ['Random sequence generation']
        self.top_k = top_k

        self.bias_domains = {'RSG': 'Random sequence generation',
                             'AC': 'Allocation concealment',
                             'BPP': 'Blinding of participants and personnel',
                             'BOA': 'Blinding of outcome assessment',
                             'IOD': 'Incomplete outcome data',
                             'SR': 'Selective reporting'
        }

        ###
        # Here we take a simple ensembling approach in which we combine the 
        # predictions made by our rationaleCNN model and the JAMIA (linear)
        # multi task variant.
        ###

        self.all_domains = ['RSG', 'AC', 'BPP', 'BOA']

        # CNN domains
        vectorizer_str = 'robotreviewer/data/keras/vectorizers/{}.pickle'
        arch_str = 'robotreviewer/data/keras/models/{}.json'
        weight_str = 'robotreviewer/data/keras/models/{}.hdf5'
        self.CNN_models = OrderedDict()
        for bias_domain in ['RSG', 'AC', 'BPP', 'BOA']:
            # Load vectorizer and keras model
            vectorizer_loc = vectorizer_str.format(bias_domain)
            arch_loc = arch_str.format(bias_domain)
            weight_loc = weight_str.format(bias_domain)
            preprocessor = pickle.load(open(vectorizer_loc, 'rb'))
            self.CNN_models[bias_domain] = RationaleCNN(preprocessor,
                                                    document_model_architecture_path=arch_loc,
                                                    document_model_weights_path=weight_loc)


        # Linear domains (these are joint models!)
        self.linear_sent_clf = MiniClassifier(robotreviewer.get_data('bias/bias_sent_level.npz'))
        self.linear_doc_clf = MiniClassifier(robotreviewer.get_data('bias/bias_doc_level.npz'))
        self.linear_vec = ModularVectorizer(norm=None, non_negative=True, binary=True, ngram_range=(1, 2), 
                                                n_features=2**26)

        #for bias_domain in ['IOD', 'SR']:
        #    self.models[bias_domain] = (vec, sent_clf, doc_clf)


    def simple_borda_count(self, a, b, weights=None):
        ''' 
        Basic Borda count implementation for just two lists. 
        Assumes that a and b are lists of indices sorted
        in *increasing* preference (so top-ranked sentence
        should be the last element).
        '''
        rank_scores_dict = defaultdict(int)

        if weights is None: 
            weights = np.ones(2)

        for i in range(len(a)):
            score = i+1 # 1 ... m
            rank_scores_dict[a[i]] += weights[0]*score
            rank_scores_dict[b[i]] += weights[1]*score 

        sorted_indices = sorted(rank_scores_dict.items(), key=operator.itemgetter(1), reverse=True)
        return [index[0] for index in sorted_indices]
        #return sorted_indices

    def annotate(self, data, top_k=None, threshold=0.5):
        """
        Annotate full text of clinical trial report
        `top_k` can be overridden here, else defaults to the class
        default set in __init__

        """
        top_k = self.top_k if not top_k else top_k

        doc_text = data.get('parsed_text')
        if not doc_text:
            return data # we've got to know the text at least..

        doc_len = len(data['text'])
        doc_sents = [sent.string for sent in doc_text.sents]

        doc_sent_start_i = [sent.start_char for sent in doc_text.sents]
        doc_sent_end_i = [sent.end_char for sent in doc_text.sents]

        structured_data = []
        #for domain, model in self.models.items():
        for domain in self.all_domains:
            ###
            # linear model predictions (all domains)
            #if type(model) == tuple: # linear model
            (vec, sent_clf, doc_clf) = (self.linear_vec, self.linear_sent_clf, self.linear_doc_clf)
            doc_domains = [self.bias_domains[domain]] * len(doc_sents)
            doc_X_i = zip(doc_sents, doc_domains)
            vec.builder_clear()
            vec.builder_add_docs(doc_sents)
            vec.builder_add_docs(doc_X_i)
            doc_sents_X = vec.builder_transform()
            doc_sents_preds = sent_clf.decision_function(doc_sents_X)
            linear_high_prob_sent_indices = np.argsort(doc_sents_preds) 

            ### 
            # CNN predictions
            bias_prob_CNN = None
            if domain in self.CNN_models:
                model = self.CNN_models[domain]
                doc = Document(doc_id=None, sentences=doc_sents) # vectorize document
                bias_prob_CNN, high_prob_sent_indices_CNN = model.predict_and_rank_sentences_for_doc(doc, num_rationales=len(doc))#num_rationales=self.m)
                    
                high_prob_sent_indices = self.simple_borda_count(high_prob_sent_indices_CNN, 
                                                                 linear_high_prob_sent_indices)[:top_k]

              
                # and now the overall (doc-level) prediction from the CNN model.
                # bias_prob = 1 --> low risk 
                # from riskofbias2:
                #        doc_y[mapped_domain] = 1 if domain["RATING"] == "YES" else -1
                #        # simplifying to LOW risk of bias = 1 *v* HIGH/UNKNOWN risk = -1
                ####
                bias_pred = int(bias_prob_CNN >= threshold) # low risk if True and high/unclear otherwise
               
            else: 
                # no aggregation here (since no CNN model for this domain)
                high_prob_sent_indices = linear_high_prob_sent_indices[-top_k:]
                high_prob_sent_indices = linear_high_prob_sent_indices[::-1] # put highest prob sentence first 
        

            #if domain == "BOA":
            #    import pdb; pdb.set_trace() 
            # high_prob_sents_CNN = [doc_sents[i] for i in high_prob_sent_indices_CNN]

            # Find high probability sentences
            high_prob_sents = [doc_sents[i] for i in high_prob_sent_indices]
            high_prob_start_i = [doc_sent_start_i[i] for i in high_prob_sent_indices]
            high_prob_end_i = [doc_sent_end_i[i] for i in high_prob_sent_indices]
            high_prob_prefixes = [doc_text.string[max(0, offset-20):offset] for offset in high_prob_start_i]
            high_prob_suffixes = [doc_text.string[offset: min(doc_len, offset+20)] for offset in high_prob_end_i]
            high_prob_sents_j = " ".join(high_prob_sents)


            # overall pred from linear model
            vec.builder_clear()
            vec.builder_add_docs([doc_text.text])
            vec.builder_add_docs([(doc_text.text, self.bias_domains[domain])])
            sent_domain_interaction = "-s-" + self.bias_domains[domain]
            vec.builder_add_docs([(high_prob_sents_j, sent_domain_interaction)])
            X = vec.builder_transform()
            bias_prob_linear = doc_clf.predict_proba(X)[0]
            
            # if we have a CNN pred, too, then average; otherwise
            # rely on linear model.
            bias_prob = bias_prob_linear
            if bias_prob_CNN is not None:
                bias_prob = (bias_prob_CNN + bias_prob_linear) / 2.0
            
            bias_pred = int(bias_prob >= threshold)

            bias_class = ["high/unclear", "low"][bias_pred] # prediction
            annotation_metadata = []
            for sent in zip(high_prob_sents, high_prob_start_i, high_prob_prefixes, high_prob_suffixes):
                sent_metadata = {"content": sent[0],
                                 "position": sent[1],
                                 "uuid": str(uuid.uuid1()),
                                 "prefix": sent[2],
                                 "suffix": sent[3]} 

                annotation_metadata.append(sent_metadata)

            structured_data.append({"domain": self.bias_domains[domain],
                                    "judgement": bias_class,
                                    "annotations": annotation_metadata})

        data.ml["bias"] = structured_data

        return data

    @staticmethod
    def get_marginalia(data):
        """
        Get marginalia formatted for Spa from structured data
        """
        marginalia = []

        for row in data['bias']:
            marginalia.append({
                        "type": "Risk of Bias",
                        "title": row['domain'],
                        "annotations": row['annotations'],
                        "description": "**Overall risk of bias prediction**: {}".format(row['judgement'])
                        })
        return marginalia

    @staticmethod
    def get_domains():
        return [u'Random sequence generation',
                u'Allocation concealment',
                u'Blinding of participants and personnel',
                u'Blinding of outcome assessment',
                u'Incomplete outcome data',
                u'Selective reporting']