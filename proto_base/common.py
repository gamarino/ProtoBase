"""
Common objects


"""
from __future__ import annotations
from typing import cast

from concurrent.futures import Future
import uuid
from abc import ABC, abstractmethod
import io
import configparser
from .exceptions import ProtoUnexpectedException, ProtoValidationException, ProtoCorruptionException, \
                        ProtoCorruptionException, \
                        ProtoNotSupportedException, ProtoNotAuthorizedException, ProtoUserException

from threading import Lock

# Constants for storage size units
KB = 1024
MB = KB * KB
GB = KB * MB
PB = KB * GB



class AtomPointer(object):
    def __init__(self, transaction_id: uuid.UUID, offset: int):
        self.transaction_id = transaction_id
        self.offset = offset


atom_class_registry = dict()

class AtomMetaclass:
    def __init__(cls, name, bases, class_dict):
        class_name = name
        if class_name != 'Atom':
            if class_name in atom_class_registry:
                raise ProtoValidationException(
                    message=f'Class repeated in atom class registry ({class_name}). Please check it')
            atom_class_registry[class_name] = cls


class AbstractSharedStorage(ABC):
    """
    ABC to solve forward type definitions
    """

    @abstractmethod
    def push_atom(self, atom: AbstractAtom) -> Future[AtomPointer]:
        """

        :param atom:
        :return:
        """

    @abstractmethod
    def get_atom(self, atom_pointer: AtomPointer) -> Future[AbstractAtom]:
        """

        :param atom_pointer:
        :return:
        """


class AbstractObjectSpace(ABC):
    """
    ABC to solve forward type definitions
    """
    storage_provider: AbstractSharedStorage

    def __init__(self, storage_provider: AbstractSharedStorage):
        self.storage_provider = storage_provider


class AbstractDatabase(ABC):
    """
    ABC to solve forward type definitions
    """
    object_space: AbstractObjectSpace

    def __init__(self, object_space: AbstractObjectSpace):
        self.object_space = object_space


class AbstractTransaction(ABC):
    """
    ABC to solve forward type definition
    """
    database: AbstractDatabase

    def __init__(self, database: AbstractDatabase):
        self.database = database


class Atom(metaclass=AtomMetaclass):
    atom_pointer: AtomPointer
    _transaction: AbstractTransaction
    _loaded: bool
    _saving: bool = False

    def __init__(self, transaction: AbstractTransaction=None, atom_pointer: AtomPointer = None, **kwargs):
        self._transaction = transaction
        self.atom_pointer = atom_pointer
        self._loaded = False
        for name, value in kwargs:
            setattr(self, name, value)

    def _load(self):
        if not self._loaded:
            if not self._transaction:
                raise ProtoValidationException(
                    message=f'An DBObject can only be instanciated within a given transaction!'
                )

            if self._transaction and \
               self.atom_pointer.transaction_id and \
               self.atom_pointer.offset:
                self._transaction.database.object_space.storage_provider.get_atom(self.atom_pointer).result()
            self._loaded = True

    def _save(self):
        if not self.atom_pointer and not self._saving:
            # It's a new object

            if self._transaction:
                # Push the object tree downhill, avoiding recursion loops
                # converting attributes strs to Literals
                self._saving = True
                for name, value in self.__dict__.items():
                    if isinstance(value, Atom):
                        if not value._transaction:
                            value._transaction = self._transaction
                        value._save()
                    elif isinstance(value, str):
                        new_literal = self._transaction.get_literal(value)
                        setattr(self, name, new_literal)
                        new_literal._save()

                # At this point all attributes has been flushed to storage if they are newly created
                # All attributes has valid AtomPointer values (either old or new)
                pointer = self._transaction.database.object_space.storage_provider.push_atom(self).result()
                self.atom_pointer = AtomPointer(pointer.transaction_id, pointer.offset)
            else:
                raise ProtoValidationException(
                    message=f'An DBObject can only be saved within a given transaction!'
                )

    def hash(self) -> int:
        return self.atom_pointer.transaction_id.int ^ \
               self.atom_pointer.offset


class RootObject(Atom):
    object_root: Dictionary
    literal_root: Dictionary


class BlockProvider(ABC):
    @abstractmethod
    def get_config_data(self) -> configparser.ConfigParser:
        """
        Get config data
        :return:
        """

    @abstractmethod
    def get_new_wal(self) -> tuple[uuid.UUID, int]:
        """
        Get a WAL to use.
        It could be an old one, or a new one.

        :return: a tuple with the id of the WAL and the next offset to use
        """

    @abstractmethod
    def get_reader(self, wal_id: uuid.UUID, position: int) -> io.FileIO:
        """
        Get a streamer initialized at position in WAL file
        wal_id

        :param wal_id:
        :param position:
        :return:
        """

    @abstractmethod
    def get_writer_wal(self) -> uuid.UUID:
        """

        :return:
        """

    @abstractmethod
    def write_streamer(self, wal_id: uuid.UUID) -> io.FileIO:
        """

        :return:
        """

    def get_current_root_object(self) -> RootObject:
        """
        Read current root object from storage
        :return: the current root object
        """

    def update_root_object(self, new_root: RootObject):
        """
        Updates or create the root object in storage
        On newly created databases, this is the first
        operation to perform

        :param new_root:
        :return:
        """

    @abstractmethod
    def close_wal(self, transaction_id: uuid.UUID):
        """
        Close a previous WAL. Flush any pending data. Make all changes durable
        :return:
        """

    @abstractmethod
    def close(self):
        """
        Close the operation of the block provider. Flush any pending data to WAL. Make all changes durable
        No further operations are allowed
        :return:
        """


class SharedStorage(AbstractSharedStorage):
    """
    A SharedStorage defines the minimun set of functionality required to implement a storage interface
    A SharedStorage object represents the current use instance of a permanent storage.
    A permanent storage is a set of transactions that represent the full story of the database. If you want
    to use that database, you will use an AtomStorage object to open, update or expand the database
    All methods should return concurret.futures.Future objects, and thus, depending on the actual implementation
    provides a level of paralellism to the system
    SharedStorage object should support multithreaded and multiprocessed uses, and can be safe in a multiserver
    environment, depending on the implementation
    """

    @abstractmethod
    def read_current_root(self) -> RootObject:
        """
        Read the current root object
        :return:
        """

    @abstractmethod
    def set_current_root(self, root_pointer: RootObject):
        """
        Set the current root object
        :return:
        """

    @abstractmethod
    def flush_wal(self):
        """
        Function to be called periodically (eg 2 minutes) to ensure no pending writes to WAL
        Additionally it is assumed that previously set_current_root, so new objects created
        before that all are included in flushed data
        This will not add any delay to operations performed after the root update, that could
        or could not be part of the flushed data.
        :return:
        """


class ObjectId:
    id : int


class AbstractDBObject(Atom):
    """
    ABC to solve forward definition
    """
    _attributes: dict[str, Atom]

    def __init__(self, transaction_id: uuid.UUID = None, offset: int = 0, attributes: dict[str, Atom] = None):
        super().__init__(transaction_id=transaction_id, offset=offset)
        self._attributes = attributes


class ParentLink(Atom):
    parent_link: AbstractDBObject | None
    cls: AbstractDBObject | None


class DBObject(Atom, AbstractDBObject):
    object_id: ObjectId
    parent_link: ParentLink | None

    def __init__(self,
                 transaction_id: uuid.UUID=None,
                 offset:int = 0,
                 object_id: ObjectId=None,
                 parent_link: ParentLink=None,
                 attributes: dict[str, Atom]=None,
                 **kwargs):
        if attributes:
            self._attributes = attributes
        super().__init__(transaction_id=transaction_id, offset=offset, attributes=attributes)
        self._object_id = object_id or kwargs.pop('object_id')
        self._parent_link = parent_link or kwargs.pop('parent_link')
        self._loaded = False

        self._attributes = {}
        if '_attributes' in kwargs:
            for attribute_name, attribute_value in kwargs['_attributes'].items():
                if attribute_name.startswith('_'):
                    raise ProtoCorruptionException(
                        message=f'DBObject attribute names could not start with "_" ({attribute_name}')

                if isinstance(attribute_value, dict) and 'AtomClass' in attribute_value:
                    if not attribute_value['AtomClass'] in atom_class_registry:
                        raise ProtoCorruptionException(
                            message=f"AtomClass {attribute_value['AtomClass']} unknown!")

                    self._attributes[attribute_name] = atom_class_registry[attribute_value['AtomClass']](
                        transaction_id=attribute_value['transaction_id'],
                        offset=attribute_value['offset'],
                    )
                else:
                    self._attributes[attribute_name] = attribute_value

    def __getattr__(self, name: str):
        self._load()

        if name.startswith('_'):
            return getattr(super(), name)

        if name in self._attributes:
            return self._attributes[name]

        pl = self._parent_link
        while pl:
            if name in pl.cls._attributes:
                return pl.cls._attributes[name]
            pl = pl.parent_link

        if hasattr(self, name):
            return getattr(super(), name)

        return None

    def __setattr__(self, key, value):
        if hasattr(self, key):
            super().__setattr__(key, value)
        else:
            raise ProtoValidationException(
                message=f'ProtoBase DBObjects are inmutable! Your are trying to set attribute {key}'
            )

    def _hasattr(self, name: str):
        self._load()

        if name.startswith('_'):
            return hasattr(self, name)

        if name in self._attributes:
            return True

        pl = self.parent_link
        while pl:
            if name in pl.cls._attributes:
                return True
            pl = pl.parent_link

        return False

    def _setattr(self, name: str, value):
        self._load()

        if name.startswith('_'):
            super().__setattr__(name, value)
            return self
        else:
            attr =  self.attributes
            attr[name] = value
            return DBObject(
                object_id=self.object_id,
                transaction_id=self.transaction_id,
                parent_link=attr,
                offset=self.offset,
            )

    def _add_parent(self, new_parent: Atom):
        self._load()

        new_parent_link = ParentLink(parent_link=self._parent_link, cls=new_parent)
        return DBObject(attributes=self._attributes, parent_link=new_parent_link)


class MutableObject(DBObject):
    """

    """
    hash_key: int = 0

    def __init__(self,
                 transaction: AbstractTransaction=None,
                 atom_pointer: AtomPointer = None,
                 **kwargs: dict[str, Atom]):
        super().__init__(transaction=transaction, atom_pointer=atom_pointer, **kwargs)
        if kwargs and 'hash_key' in kwargs:
            self.hash_key = kwargs['hash_key']
        else:
            self.hash_key = uuid.uuid4().int

    def __getattr__(self, name: str):
        if not self.transaction:
            raise ProtoValidationException(
                message=f"You can't access a mutable object out of the scope of a transaction!"
            )

        current_object = self.transaction.get_mutable(self.hash_key)
        return current_object.__getattr__(name)

    def __setattr__(self, key, value):
        if hasattr(self, key):
            super().__setattr__(key, value)
        else:
            if not self._transaction:
                raise ProtoValidationException(
                    message=f'Proto MutableObjects can only be modified within the context of a transaction!'
                )
            current_object = cast(DBObject, self._transaction.get_mutable(self.hash_key))
            new_object = current_object._setattr(key, value)
            self._transaction.set_mutable(self.hash_key, new_object)

    def _hasattr(self, name: str):
        if not self.transaction:
            raise ProtoValidationException(
                message=f"You can't access a mutable object out of the scope of a transaction!"
            )

        current_object = self.transaction.get_mutable(self.hash_key)
        return current_object.hasattr(name)

    def _setattr(self, name: str, value):
        if not self.transaction:
            raise ProtoValidationException(
                message=f"You can't access a mutable object out of the scope of a transaction!"
            )

        current_object = self.transaction.get_mutable(self.hash_key)
        new_object = current_object._setattr(name, value)
        self.transaction.set_mutable(self.hash_key, new_object)
        return self

    def _add_parent(self, new_parent: Atom):
        if not self.transaction:
            raise ProtoValidationException(
                message=f"You can't access a mutable object out of the scope of a transaction!"
            )

        current_object = self.transaction.get_mutable(self.hash_key)
        new_object = current_object._add_parent(new_parent)
        self._transaction.set_mutable(self.hash_key, new_object)
        return self

    def _load(self):
        pass

    def _save(self):
        pass

    def hash(self) -> int:
        return self.hash_key


class DBCollections(Atom):
    indexes: dict[str, Atom] | None
    count: int = 0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.indexes = {}

    @abstractmethod
    def as_iterable(self) -> list[Atom]:
        """

        :return:
        """


class Literal(Atom):
    string:str

    def __init__(self,
                 transaction_id: uuid.UUID=None,
                 offset:int = 0,
                 literal: str = None,
                 **kwargs):
        super().__init__(transaction_id=transaction_id, offset=offset)
        self.string = literal or kwargs.pop('literal')

    def __eq__(self, other:str | Literal) -> bool:
        if isinstance(other, Literal):
            return self.string == other.string
        else:
            return self.string == other

    def __str__(self) -> str:
        return self.string

    def __add__(self, other:str | Literal) -> Literal :
        if isinstance(other, Literal):
            return Literal(literal=self.string + other.string)
        else:
            return Literal(literal=self.string + other)

class ObjectSpace(AbstractObjectSpace):
    storage: SharedStorage
    _lock: Lock

    def __init__(self, storage: SharedStorage):
        super().__init__(storage)
        self.storage = storage
        self._lock = Lock()

    def open_database(self, database_name: str) -> Database:
        """
        Opens a database
        :return:
        """
        with self._lock:
            root = self.storage.read_current_root()
            if root:
                db_catalog: Dictionary = cast(Dictionary, root.object_root)
                if db_catalog.has(database_name):
                    return Database(self, database_name)

            raise ProtoValidationException(
                message=f'Database {database_name} does not exist!'
            )

    def new_database(self, database_name: str) -> Database:
        """
        Opens a database
        :return:
        """
        with self._lock:
            root = self.storage.read_current_root()
            if not root:
                root = RootObject()
                root.object_root = Dictionary()
                root.literal_root = Dictionary()

            db_catalog: Dictionary = cast(Dictionary, root.object_root)
            if not db_catalog.has(database_name):
                new_db = Database(self, database_name)
                new_db_root = Dictionary()
                new_db_catalog = db_catalog.set_at(database_name, new_db_root)
                new_db_catalog._save()
                root.object_root = new_db_catalog
                self.storage.set_current_root(root)
                return new_db

            raise ProtoValidationException(
                message=f'Database {database_name} already exists!'
            )

    def rename_database(self, old_name: str, new_name: str):
        """
        Rename an existing database. If database is already opened, if will not
        commit any more!
        :return:
        """
        with self._lock:
            root = self.storage.read_current_root()
            if root:
                db_catalog: Dictionary = cast(Dictionary, root.object_root)
                if db_catalog.has(old_name):
                    database_root: Dictionary = cast(Dictionary, db_catalog.get_at(old_name))
                    new_db_catalog: Dictionary = db_catalog.remove_key(old_name)
                    new_db_catalog = new_db_catalog.set_at(new_name, database_root)
                    new_db_catalog._save()
                    root.object_root = new_db_catalog
                    self.storage.set_current_root(root)

            raise ProtoValidationException(
                message=f'Database {old_name} does not exist!'
            )

    def get_literals(self, literals: list[str]) -> dict[str, Atom]:
        with self._lock:
            root = self.storage.read_current_root()
            literal_catalog: Dictionary = cast(Dictionary, root.literal_root)
            result = {}
            for literal in literals:
                if literal_catalog.has(literal):
                    result[literal] = literal_catalog.get_at(literal)
                else:
                    new_literal = Literal(literal)
                    result[literal] = new_literal
                    literal_catalog.set_at(literal, new_literal)

            root.literal_root = literal_catalog
            self.storage.set_current_root(root)

            return result

    def commit_database(self, database_name:str, new_root: Atom):
        with self._lock:
            root = self.storage.read_current_root()
            db_catalog: Dictionary = cast(Dictionary, root.object_root)
            if db_catalog.has(database_name):
                new_db_catalog = db_catalog.set_at(database_name, new_root)
                new_db_catalog._save()
                root.object_root = new_db_catalog
                self.storage.set_current_root(root)
            else:
                raise ProtoValidationException(
                    message=f'Database {database_name} does not exist!'
                )


class Database(AbstractDatabase):
    database_name: str
    object_space: ObjectSpace

    def __init__(self, object_space: ObjectSpace, database_name: str):
        super().__init__(object_space)
        self.object_space = object_space
        self.database_name = database_name

    def new_transaction(self) -> ObjectTransaction:
        """
        Start a new read transaction
        :return:
        """
        root = self.object_space.storage.read_current_root()
        db_catalog: Dictionary = cast(Dictionary, root.object_root)
        if db_catalog.has(self.database_name):
            current_root: Dictionary = cast(Dictionary, db_catalog.get_at(self.database_name))
            return ObjectTransaction(self, current_root)

    def new_branch_database(self) -> Database:
        """
        Gets a new database, derived from the current state of the origin database.
        The derived database could be modified in an idependant history.
        Transactions in the derived database will not impact in the origin database
        :return:
        """

        root = self.object_space.storage.read_current_root()
        db_catalog: Dictionary = cast(Dictionary, root.object_root)
        new_db_name = uuid.uuid4().hex
        new_db = Database(self.object_space, new_db_name)

        self.object_space.commit_database(self.database_name, Dictionary())
        return new_db

    def get_literal(self, string: str):
        root = self.object_space.storage.read_current_root()
        if root.literal_root.has(string):
            return root.literal_root.get_at(string)
        else:
            return None


class ObjectTransaction(AbstractTransaction):
    initial_transaction_root: Dictionary = None
    transaction_root: Dictionary = None
    new_roots: dict[str, Atom] = None
    read_objects:dict[int, Atom] = None
    read_lock_objects: dict[int, Atom] = None
    mutable_objects: HashDictionary = None
    initial_mutable_root: HashDictionary = None
    new_literals: Dictionary = None

    def __init__(self, database: Database, transaction_root: Dictionary):
        super().__init__(database)
        if self.transaction_root:
            self.transaction_root = transaction_root
        else:
            self.transaction_root = Dictionary()
        self.initial_transaction_root = transaction_root
        self.new_roots = {}
        self.read_objects = {}
        self.read_lock_objects = {}
        if transaction_root.has('_mutable_root'):
            self.mutable_objects = cast(HashDictionary, self.transaction_root.get_at('_mutable_root'))
            self.initial_mutable_root = self.mutable_objects
        else:
            self.mutable_objects = HashDictionary()
        self.new_literals = Dictionary(transaction=self)

    def get_literal(self, string: str):
        if self.new_literals.has(string):
            return self.new_literals.get_at(string)
        else:
            existing_literal = self.database.get_literal(string)
            if existing_literal:
                return existing_literal
            else:
                new_literal = Literal(transaction=self, string=string)
                self.new_literals = self.new_literals.set_at(string, new_literal)
                return new_literal

    def get_root_object(self, name: str) -> DBObject | None:
        """
        Get a root object from the root catalog

        :param name:
        :return:
        """
        return self.transaction_root.get_at(name)

    def set_root_object(self, name: str, value: Atom) -> DBObject | None:
        """
        Set a root object into the root catalog. It is the only way to persist changes

        :param name:
        :param value:
        :return:
        """
        self.transaction_root = self.transaction_root.set_at(name, value)

    def set_lock_object(self, object: DBObject):
        self.read_lock_objects[object.hash()] = object

    def commit(self, return_object: Atom = None):
        """
        Close the transaction and make it persistent. All changes recorded
        Before commit all checked and modified objects will be tested if modified
        by another transaction. Transaction will proceed only if no change in
        used objects is verified.
        If a return object is specified, the full tree of related objects is persisted
        All created objects, not reachable from this return_object or any updated root
        will NOT BE PERSISTED, and they will be not usable after commit!
        :return:
        """

    def abort(self):
        """
        Discard any changes made. Database is not modified. All created objects are no longer usable
        :return:
        """

    def _get_string_hash(self, string: str) -> int:
        """

        :param string:
        :return: a hash based in db persisted strings
        """
        return self.database.get_literal(string)

    def get_mutable(self, key:int):
        return self.mutable_objects.get_at(key)

    def set_mutable(self, key:int, value:Atom):
        return self.mutable_objects.set_at(key, value)

    def new_hash_dictionary(self) -> HashDictionary :
        """
        Return a new HashDictionary conected to this transaction
        :return:
        """
        return HashDictionary(transaction=self)

    def new_dictionary(self) -> Dictionary:
        """
        Return a new Dictionary conected to this transaction

        :return:
        """
        return Dictionary(transaction=self)

    def new_list(self) -> List:
        """
        Return a new List connected to this transaction
        :return:
        """
        return List(transaction=self)

    def new_set(self) -> Set:
        """
        Return a new Set connected to this transaction
        :return:
        """
        return Set(transaction=self)