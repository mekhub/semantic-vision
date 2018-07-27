import logging
import jpype
import numpy as np
import datetime
import opencog.logger
import argparse

from opencog.atomspace import AtomSpace, TruthValue, types
from opencog.type_constructors import *
from opencog.scheme_wrapper import *

from utils import *
from interface import FeatureLoader, AnswerHandler
from multidnn import NetsVocabularyNeuralNetworkRunner
from hypernet import HyperNetNeuralNetworkRunner

### Reusable code (no dependency on global vars)

def initializeRootAndOpencogLogger(opencogLogLevel, pythonLogLevel):
    opencog.logger.log.set_level(opencogLogLevel)
    
    rootLogger = logging.getLogger()
    rootLogger.setLevel(pythonLogLevel)
    rootLogger.addHandler(logging.StreamHandler())

def initializeAtomspace(atomspaceFileName = None):
    atomspace = scheme_eval_as('(cog-atomspace)')
    scheme_eval(atomspace, '(use-modules (opencog))')
    scheme_eval(atomspace, '(use-modules (opencog exec))')
    scheme_eval(atomspace, '(use-modules (opencog query))')
    scheme_eval(atomspace, '(add-to-load-path ".")')
    if atomspaceFileName is not None:
        scheme_eval(atomspace, '(load-from-path "' + atomspaceFileName + '")')

    return atomspace

def pushAtomspace(parentAtomspace):
    scheme_eval(parentAtomspace, '(cog-push-atomspace)')
    childAtomspace = scheme_eval_as('(cog-atomspace)')
    set_type_ctor_atomspace(childAtomspace)
    return childAtomspace

def popAtomspace(childAtomspace):
    scheme_eval(childAtomspace, '(cog-pop-atomspace)')
    parentAtomspace = scheme_eval_as('(cog-atomspace)')
    set_type_ctor_atomspace(parentAtomspace)
    return parentAtomspace

class TsvFileFeatureLoader(FeatureLoader):
    
    def __init__(self, featuresPath, featuresPrefix):
        self.featuresPath = featuresPath
        self.featuresPrefix = featuresPrefix
        
    def getFeaturesFileName(self, imageId):
        return self.featuresPrefix + addLeadingZeros(imageId, 12) + '.tsv'

    def loadFeaturesUsingFileHandle(self, fileHandle):
        featuresByBoundingBoxIndex = []
        next(fileHandle)
        for line in fileHandle:
            features = [float(number) for number in line.split()]
            featuresByBoundingBoxIndex.append(features[10:])
        return featuresByBoundingBoxIndex
    
    def loadFeaturesByFileName(self, featureFileName):
        return loadDataFromZipOrFolder(self.featuresPath, featureFileName, 
            lambda fileHandle: self.loadFeaturesUsingFileHandle(fileHandle))
        
    def loadFeaturesByImageId(self, imageId):
        return self.loadFeaturesByFileName(self.getFeaturesFileName(imageId))
    
class StatisticsAnswerHandler(AnswerHandler):
    
    def __init__(self):
        self.logger = logging.getLogger('StatisticsAnswerHandler')
        self.processedQuestions = 0
        self.questionsAnswered = 0
        self.correctAnswers = 0

    def onNewQuestion(self, record):
        self.processedQuestions += 1

    def onAnswer(self, record, answer):
        self.questionsAnswered += 1
        if answer == record.answer:
            self.correctAnswers += 1
        self.logger.debug('Correct answers %s%%', self.correctAnswerPercent())

    def correctAnswerPercent(self):
        return self.correctAnswers / self.questionsAnswered * 100

### Pipeline code

def runNeuralNetwork(boundingBox, conceptNode):
    try:
        logger = logging.getLogger('runNeuralNetwork')
        logger.debug('runNeuralNetwork: %s, %s', boundingBox.name, conceptNode.name)
        
        featuresValue = boundingBox.get_value(PredicateNode('features'))
        if featuresValue is None:
            logger.error('no features found, return FALSE')
            return TruthValue(0.0, 1.0)
        features = np.array(featuresValue.to_list())
        word = conceptNode.name
        
        global neuralNetworkRunner
        resultTensor = neuralNetworkRunner.runNeuralNetwork(features, word)
        result = resultTensor.item()
        
        logger.debug('bb: %s, word: %s, result: %s', boundingBox.name, word, str(result))
        # Return matching values from PatternMatcher by adding 
        # them to bounding box and concept node
        # TODO: how to return predicted values properly?
        boundingBox.set_value(conceptNode, FloatValue(result))
        conceptNode.set_value(boundingBox, FloatValue(result))
        return TruthValue(result, 1.0)
    except BaseException as e:
        logger.error('Unexpected exception %s', e)
        return TruthValue(0.0, 1.0)

class OtherDetSubjObjResult:
    
    def __init__(self, bb, attribute, object):
        self.bb = bb
        self.attribute = attribute
        self.object = object
        self.attributeProbability = bb.get_value(attribute).to_list()[0]
        self.objectProbability = bb.get_value(object).to_list()[0]
        
    def __lt__(self, other):
        if abs(self.objectProbability - other.objectProbability) > 0.000001:
            return self.objectProbability < other.objectProbability
        else:
            return self.attributeProbability < other.attributeProbability

    def __gt__(self, other):
        return other.__lt__(self);

    def __str__(self):
        return '{} is {} {}({}), score = {}'.format(self.bb.name,
                                   self.attribute.name,
                                   self.object.name,
                                   self.objectProbability,
                                   self.attributeProbability)

class PatternMatcherVqaPipeline:
    
    def __init__(self, featureLoader, questionConverter, atomspace, answerHandler):
        self.logger = logging.getLogger('PatternMatcherVqaPipeline')
        self.featureLoader = featureLoader
        self.questionConverter = questionConverter
        self.atomspace = atomspace
        self.answerHandler = answerHandler

    # TODO: pass atomspace as parameter to exclude necessity of set_type_ctor_atomspace
    def addBoundingBoxesIntoAtomspace(self, record):
        boundingBoxNumber = 0
        for boundingBoxFeatures in self.featureLoader.loadFeaturesByImageId(record.imageId):
            imageFeatures = FloatValue(boundingBoxFeatures)
            boundingBoxInstance = ConceptNode(
                'BoundingBox-' + str(boundingBoxNumber))
            InheritanceLink(boundingBoxInstance, ConceptNode('BoundingBox'))
            boundingBoxInstance.set_value(PredicateNode('features'), imageFeatures)
            boundingBoxNumber += 1
    
    def answerQuestion(self, record):
        self.logger.debug('processing question: %s', record.question)
        self.answerHandler.onNewQuestion(record)
        self.atomspace = pushAtomspace(self.atomspace)
        try:
            
            self.addBoundingBoxesIntoAtomspace(record)
            
            relexFormula = self.questionConverter.parseQuestion(record.question)
            queryInScheme = self.questionConverter.convertToOpencogScheme(relexFormula)
            if queryInScheme is None:
                self.logger.error('Question was not parsed')
                return
            self.logger.debug('Scheme query: %s', queryInScheme)
        
            if record.questionType == 'yes/no':
                answer = self.answerYesNoQuestion(queryInScheme)
            else:
                answer = self.answerOtherQuestion(queryInScheme)
            
            self.answerHandler.onAnswer(record, answer)
            
            print('{}::{}::{}::{}::{}'.format(record.questionId, record.question, 
                answer, record.answer, record.imageId))
            
        finally:
            self.atomspace = popAtomspace(self.atomspace)
    
    def answerYesNoQuestion(self, queryInScheme):
        evaluateStatement = '(cog-evaluate! ' + queryInScheme + ')'
        start = datetime.datetime.now()
        result = scheme_eval_v(self.atomspace, evaluateStatement)
        delta = datetime.datetime.now() - start
        self.logger.debug('The result of pattern matching is: '
                          '%s, time: %s microseconds',
                          result, delta.microseconds)
        answer = 'yes' if result.to_list()[0] >= 0.5 else 'no'
        return answer
    
    def answerOtherQuestion(self, queryInScheme):
        evaluateStatement = '(cog-execute! ' + queryInScheme + ')'
        start = datetime.datetime.now()
        resultsData = scheme_eval_h(self.atomspace, evaluateStatement)
        delta = datetime.datetime.now() - start
        self.logger.debug('The resultsData of pattern matching contains: '
                          '%s records, time: %s microseconds',
                          len(resultsData.out), delta.microseconds)
        
        results = []
        for resultData in resultsData.out:
            out = resultData.out
            results.append(OtherDetSubjObjResult(out[0], out[1], out[2]))
        results.sort(reverse = True)
        
        for result in results:
            self.logger.debug(str(result))
        
        maxResult = results[0]
        answer = maxResult.attribute.name
        return answer
    
    def answerSingleQuestion(self, question, imageId):
        questionRecord = recordModule.Record()
        questionRecord.question = question
        questionRecord.imageId = imageId
        self.answerQuestion(questionRecord)
    
    def answerQuestionsFromFile(self, questionsFileName):
        questionFile = open(questionsFileName, 'r')
        for line in questionFile:
            try:
                record = recordModule.Record.fromString(line)
                self.answerQuestion(record)
            except BaseException as e:
                logger.error('Unexpected exception %s', e)
                continue
    
### MAIN

question2atomeseLibraryPath = (currentDir(__file__) +
    '/../question2atomese/target/question2atomese-1.0-SNAPSHOT.jar')

parser = argparse.ArgumentParser(description='Load pretrained words models '
   'and answer questions using OpenCog PatternMatcher')
parser.add_argument('--kind', '-k', dest='kindOfModel',
    action='store', type=str, required=True,
    choices=['MULTIDNN', 'HYPERNET'],
    help='model kind: (1) MULTIDNN requires --model parameter only; '
    '(2) HYPERNET requires --model, --words and --embedding parameters')
parser.add_argument('--questions', '-q', dest='questionsFileName',
    action='store', type=str, required=True,
    help='parsed questions file name')
parser.add_argument('--models', '-m', dest='modelsFileName',
    action='store', type=str, required=True,
    help='models file name')
parser.add_argument('--features', '-f', dest='featuresPath',
    action='store', type=str, required=True,
    help='features path (it can be either zip archive or folder name)')
parser.add_argument('--words', '-w', dest='wordsFileName',
    action='store', type=str,
    help='words dictionary')
parser.add_argument('--embeddings', '-e', dest='wordEmbeddingsFileName',
    action='store', type=str,
    help='word embeddings')
parser.add_argument('--features-prefix', dest='featuresPrefix',
    action='store', type=str, default='val2014_parsed_features/COCO_val2014_',
    help='features prefix to be merged with path to open feature')
parser.add_argument('--atomspace', '-a', dest='atomspaceFileName',
    action='store', type=str,
    help='Scheme program to fill atomspace with facts')
parser.add_argument('--opencog-log-level', dest='opencogLogLevel',
    action='store', type = str, default='NONE',
    choices=['FINE', 'DEBUG', 'INFO', 'ERROR', 'NONE'],
    help='OpenCog logging level')
parser.add_argument('--python-log-level', dest='pythonLogLevel',
    action='store', type = str, default='INFO',
    choices=['INFO', 'DEBUG', 'ERROR'], 
    help='Python logging level')
parser.add_argument('--question2atomese-java-library',
    dest='q2aJarFilenName', action='store', type = str,
    default=question2atomeseLibraryPath,
    help='path to question2atomese-<version>.jar')
args = parser.parse_args()

# global variables
neuralNetworkRunner = None

initializeRootAndOpencogLogger(args.opencogLogLevel, args.pythonLogLevel)

logger = logging.getLogger('PatternMatcherVqaTest')
logger.info('VqaMainLoop started')

# import record
recordModule = importModuleFromFile('record',
              currentDir(__file__) + '/../question2atomese/record.py')

jpype.startJVM(jpype.getDefaultJVMPath(), 
               '-Djava.class.path=' + str(args.q2aJarFilenName))
try:
    
    featureLoader = TsvFileFeatureLoader(args.featuresPath, args.featuresPrefix)
    questionConverter = jpype.JClass('org.opencog.vqa.relex.QuestionToOpencogConverter')()
    atomspace = initializeAtomspace(args.atomspaceFileName)
    statisticsAnswerHandler = StatisticsAnswerHandler()
    
    if (args.kindOfModel == 'MULTIDNN'):
        neuralNetworkRunner = NetsVocabularyNeuralNetworkRunner(args.modelsFileName)
    elif (args.kindOfModel == 'HYPERNET'):
        neuralNetworkRunner = HyperNetNeuralNetworkRunner(args.wordsFileName,
                        args.wordEmbeddingsFileName, args.modelsFileName)
    
    pmVqaPipeline = PatternMatcherVqaPipeline(featureLoader,
                                              questionConverter,
                                              atomspace,
                                              statisticsAnswerHandler)
    pmVqaPipeline.answerQuestionsFromFile(args.questionsFileName)
    
    print('Questions processed: {}, answered: {}, correct answers: {}% ({})'
          .format(statisticsAnswerHandler.processedQuestions,
                  statisticsAnswerHandler.questionsAnswered,
                  statisticsAnswerHandler.correctAnswerPercent(),
                  statisticsAnswerHandler.correctAnswers))
finally:
    jpype.shutdownJVM()

logger.info('VqaMainLoop stopped')
