
from .expression import BitVecVariable, BoolVariable, ArrayVariable, Array, Bool, BitVec, BoolConstant, ArrayProxy, BoolEq, Variable, Constant
from .visitors import GetDeclarations, TranslatorSmtlib, get_variables, simplify, replace, translate_to_smtlib
import logging

logger = logging.getLogger(__name__)


class ConstraintSet(object):
    ''' Constraint Sets

        An object containing a set of constraints. Serves also as a factory for
        new variables.
    '''

    def __init__(self):
        self._constraints = list()
        self._parent = None
        self._sid = 0
        self._declarations = {}
        self._child = None

    def __reduce__(self):
        return (self.__class__, (), {'_parent': self._parent, '_constraints': self._constraints, '_sid': self._sid, '_declarations': self._declarations})

    def __enter__(self):
        assert self._child is None
        self._child = self.__class__()
        self._child._parent = self
        self._child._sid = self._sid
        self._child._declarations = dict(self._declarations)
        return self._child

    def __exit__(self, ty, value, traceback):
        self._child._parent = None
        self._child = None

    def __len__(self):
        if self._parent is not None:
            return len(self._constraints) + len(self._parent)
        return len(self._constraints)

    def add(self, constraint, check=False):
        '''
        Add a constraint to the set

        :param constraint: The constraint to add to the set.
        :param check: Currently unused.
        :return:
        '''
        # XXX(yan): check is an unused param
        if isinstance(constraint, bool):
            constraint = BoolConstant(constraint)
        assert isinstance(constraint, Bool)
        constraint = simplify(constraint)
        # If self._child is not None this constraint set has been forked and a
        # a derived constraintset may be using this. So we can't add any more
        # constraints to this one. After the child constraintSet is deleted
        # we regain the ability to add constraints.
        if self._child is not None:
            raise Exception('ConstraintSet is frozen')

        if isinstance(constraint, BoolConstant):
            if not constraint.value:
                logger.info("Adding an imposible constant constraint")
                self._constraints = [constraint]
            else:
                return

        self._constraints.append(constraint)

    def _get_sid(self):
        ''' Returns an unique id. '''
        assert self._child is None
        self._sid += 1
        return self._sid

    def __get_related(self, related_to=None):
        if related_to is not None:
            number_of_constraints = len(self.constraints)
            remaining_constraints = set(self.constraints)
            related_variables = get_variables(related_to)
            related_constraints = set()

            added = True
            while added:
                added = False
                logger.debug('Related variables %r', [x.name for x in related_variables])
                for constraint in list(remaining_constraints):
                    if isinstance(constraint, BoolConstant):
                        if constraint.value:
                            continue
                        else:
                            related_constraints = {constraint}
                            break

                    variables = get_variables(constraint)
                    if related_variables & variables:
                        remaining_constraints.remove(constraint)
                        related_constraints.add(constraint)
                        related_variables |= variables
                        added = True

            logger.debug('Reduced %d constraints!!', number_of_constraints - len(related_constraints))
        else:
            related_variables = set()
            for constraint in self.constraints:
                related_variables |= get_variables(constraint)
            related_constraints = set(self.constraints)
        return related_variables, related_constraints

    def to_string(self, related_to=None, replace_constants=True):
        related_variables, related_constraints = self.__get_related(related_to)

        if replace_constants:
            constant_bindings = {}
            for expression in self.constraints:
                if isinstance(expression, BoolEq) and \
                   isinstance(expression.operands[0], Variable) and \
                   isinstance(expression.operands[1], Constant):
                    constant_bindings[expression.operands[0]] = expression.operands[1]

        tmp = set()
        result = ''
        for var in related_variables:
            # FIXME
            # band aid hack around the fact that we are double declaring stuff :( :(
            if var.declaration in tmp:
                logger.warning("Variable '%s' was copied twice somewhere", var.name)
                continue
            tmp.add(var.declaration)
            result += var.declaration + '\n'

        translator = TranslatorSmtlib(use_bindings=True)
        for constraint in related_constraints:
            if replace_constants:
                if isinstance(constraint, BoolEq) and \
                   isinstance(constraint.operands[0], Variable) and \
                   isinstance(constraint.operands[1], Constant):
                    var = constraint.operands[0]
                    expression = constraint.operands[1]
                    expression = simplify(replace(expression, constant_bindings))
                    constraint = var == expression

            translator.visit(constraint)
        for name, exp, smtlib in translator.bindings:
            if isinstance(exp, BitVec):
                result += '(declare-fun %s () (_ BitVec %d))' % (name, exp.size)
            elif isinstance(exp, Bool):
                result += '(declare-fun %s () Bool)' % name
            elif isinstance(exp, Array):
                result += '(declare-fun %s () (Array (_ BitVec %d) (_ BitVec %d)))' % (name, exp.index_bits, exp.value_bits)
            else:
                raise Exception("Type not supported %r", exp)
            result += '(assert (= %s %s))\n' % (name, smtlib)

        constraint_str = translator.pop()
        while constraint_str is not None:
            if constraint_str != 'true':
                result += '(assert %s)\n' % constraint_str
            constraint_str = translator.pop()
        return result

    def _declare(self, var):
        ''' Declare the variable `var` '''
        if var.name in self._declarations:
            raise ValueError('Variable already declared')
        self._declarations[var.name] = var
        return var

    def get_variable(self, name):
        ''' Returns the variable declared under name or None if it does not exists '''
        if name not in self._declarations:
            return None
        return self._declarations[name]

    def get_declared_variables(self):
        ''' Returns the variable expressions of this constraint set '''
        return self._declarations.values()

    @property
    def declarations(self):
        ''' Returns the variable expressions of this constraint set '''
        declarations = GetDeclarations()
        for a in self.constraints:
            try:
                declarations.visit(a)
            except BaseException:
                # there recursion limit exceeded problem,
                # try a slower, iterative solution
                #logger.info('WARNING: using iterpickle to dump recursive expression')
                #from utils import iterpickle
                #file('recursive.pkl', 'w').write(iterpickle.dumps(a))
                raise
        return declarations.result

    @property
    def constraints(self):
        '''
        :rtype tuple
        :return: All constraints represented by this and parent sets.
        '''
        if self._parent is not None:
            return tuple(self._constraints) + self._parent.constraints
        return tuple(self._constraints)

    def __iter__(self):
        return iter(self.constraints)

    def __str__(self):
        ''' Returns a smtlib representation of the current state '''
        return self.to_string()

    def _make_unique_name(self, name='VAR'):
        ''' Makes an uniq variable name'''
        # the while loop is necessary because appending the result of _get_sid()
        # is not guaranteed to make a unique name on the first try; a colliding
        # name could have been added previously
        while name in self._declarations:
            name = '%s_%d' % (name, self._get_sid())
        return name

    def migrate(self, expression, name_migration_map=None):
        ''' Migrate an expression created for a different constraint set to self.
            Returns an expression that can be used with this constraintSet

            All the foreign variables used in the expression are replaced by
            variables of this constraint set. If the variable was replaced before
            the replacement is taken from the provided migration map.

            The migration mapping is updated with new replacements.

            ```
            from manticore.core.smtlib import *

            cs1 = ConstraintSet()
            cs2 = ConstraintSet()
            var1 = cs1.new_bitvec(32, 'var')
            var2 = cs2.new_bitvec(32, 'var')
            cs1.add(Operators.ULT(var1, 3)) # var1 can be 0, 1, 2

            # make a migration map dict
            name_migration_map1 = {}

            # this expression is composed with variables of both cs
            expression = var1 > var2
            migrated_expression = cs1.migrate(expression, name_migration_map1)
            cs1.add(migrated_expression)


            expression = var2 > 0
            migrated_expression = cs1.migrate(expression, name_migration_map1)
            cs1.add(migrated_expression)

            print (cs1)
            print (solver.check(cs1))
            print (solver.get_all_values(cs1, var1)) # should only be [2]
            ```

            :param expression: the potentially foreign expression
            :param name_migration_map: a name to name mapping of already migrated variables
            :return: a migrated expresion where all the variables are fresh BoolVariable

        '''
        if name_migration_map is None:
            name_migration_map = {}

        #  name_migration_map -> object_migration_map
        #  Based on the name mapping in name_migration_map build an object to
        #  object mapping to be used in the replacing of variables
        #  inv: object_migration_map's keys should ALWAYS be external/foreign
        #  expressions, and its values should ALWAYS be internal/local expressions
        object_migration_map = {}

        for expression_var in get_variables(expression):

            # do nothing if it is a known/declared variable object
            if any(expression_var is x for x in self.get_declared_variables()):
                continue

            # If a variable with the same name was previously migrated
            if expression_var.name in name_migration_map:
                migrated_name = name_migration_map[expression_var.name]
                native_var = self.get_variable(migrated_name)
                if native_var is None:
                    raise Exception("name_migration_map contains an unknown variable")
                object_migration_map[expression_var] = native_var
                #continue if there is already a migrated variable for it
                continue

            # expression_var was not found in the local declared variables nor
            # any variable with the dsame name was previously migrated
            # lets make a new uniq internal name for it
            migrated_name = expression_var.name
            if migrated_name in self._declarations:
                migrated_name = self._make_unique_name(expression_var.name + '_migrated')
            # Create and declare a new variable of given type
            if isinstance(expression_var, Bool):
                new_var = self.new_bool(name=migrated_name)
            elif isinstance(expression_var, BitVec):
                new_var = self.new_bitvec(expression_var.size, name=migrated_name)
            elif isinstance(expression_var, Array):
                # Note that we are discarding the ArrayProxy encapsulation 
                new_var = self.new_array(index_max=expression_var.index_max, index_bits=expression_var.index_bits, value_bits=expression_var.value_bits, name=migrated_name).array
            else:
                raise NotImplemented("Unknown expression type {} encountered during expression migration".format(type(var)))
            # Update the var to var mapping
            object_migration_map[expression_var] = new_var
            # Update the name to name mapping
            name_migration_map[expression_var.name] = new_var.name

        #  Actually replace each appearence of migrated variables by the new ones
        migrated_expression = replace(expression, object_migration_map)
        return migrated_expression

    def new_bool(self, name=None, taint=frozenset(), avoid_collisions=False):
        ''' Declares a free symbolic boolean in the constraint store
            :param name: try to assign name to internal variable representation,
                         if not uniq a numeric nonce will be appended
            :param avoid_collisions: potentially avoid_collisions the variable to avoid name colisions if True
            :return: a fresh BoolVariable
        '''
        if name is None:
            name = 'B'
            avoid_collisions = True
        if avoid_collisions:
            name = self._make_unique_name(name)
        if not avoid_collisions and name in self._declarations:
            raise ValueError("Name already used")
        var = BoolVariable(name, taint=taint)
        return self._declare(var)

    def new_bitvec(self, size, name=None, taint=frozenset(), avoid_collisions=False):
        ''' Declares a free symbolic bitvector in the constraint store
            :param size: size in bits for the bitvector
            :param name: try to assign name to internal variable representation,
                         if not uniq a numeric nonce will be appended
            :param avoid_collisions: potentially avoid_collisions the variable to avoid name colisions if True
            :return: a fresh BitVecVariable
        '''
        if not (size == 1 or size % 8 == 0):
            raise Exception('Invalid bitvec size %s' % size)
        if name is None:
            name = 'BV'
            avoid_collisions = True
        if avoid_collisions:
            name = self._make_unique_name(name)
        if not avoid_collisions and name in self._declarations:
            raise ValueError("Name already used")
        var = BitVecVariable(size, name, taint=taint)
        return self._declare(var)

    def new_array(self, index_bits=32, name=None, index_max=None, value_bits=8, taint=frozenset(), avoid_collisions=False):
        ''' Declares a free symbolic array of value_bits long bitvectors in the constraint store.
            :param index_bits: size in bits for the array indexes one of [32, 64]
            :param value_bits: size in bits for the array values
            :param name: try to assign name to internal variable representation,
                         if not uniq a numeric nonce will be appended
            :param index_max: upper limit for indexes on ths array (#FIXME)
            :param avoid_collisions: potentially avoid_collisions the variable to avoid name colisions if True
            :return: a fresh ArrayProxy
        '''
        if name is None:
            name = 'A'
            avoid_collisions = True
        if avoid_collisions:
            name = self._make_unique_name(name)
        if not avoid_collisions and name in self._declarations:
            raise ValueError("Name already used")
        var = self._declare(ArrayVariable(index_bits, index_max, value_bits, name, taint=taint))
        return ArrayProxy(var)
