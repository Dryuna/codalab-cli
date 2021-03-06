'''
BundleModel is a wrapper around database calls to save and load bundle metadata.
'''
from sqlalchemy import (
    and_,
    or_,
    not_,
    select,
    union,
    desc,
    func,
)
from sqlalchemy.exc import (
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.sql.expression import (
    literal,
    true,
)

from codalab.bundles import get_bundle_subclass
from codalab.common import (
    IntegrityError,
    precondition,
    UsageError,
    State,
)
from codalab.lib import (
    spec_util,
    worksheet_util,
)
from codalab.model.util import LikeQuery
from codalab.model.tables import (
    bundle as cl_bundle,
    bundle_dependency as cl_bundle_dependency,
    bundle_metadata as cl_bundle_metadata,
    bundle_action as cl_bundle_action,
    group as cl_group,
    group_bundle_permission as cl_group_bundle_permission,
    group_object_permission as cl_group_worksheet_permission,
    GROUP_OBJECT_PERMISSION_ALL,
    GROUP_OBJECT_PERMISSION_READ,
    GROUP_OBJECT_PERMISSION_NONE,
    user_group as cl_user_group,
    worksheet as cl_worksheet,
    worksheet_item as cl_worksheet_item,
    db_metadata,
)
from codalab.objects.worksheet import (
    item_sort_key,
    Worksheet,
)
from codalab.objects.permission import parse_permission

import re, collections

CONDITION_REGEX = re.compile('^([\.\w/]+)=(.*)$')

class BundleModel(object):
    def __init__(self, engine):
        '''
        Initialize a BundleModel with the given SQLAlchemy engine.
        '''
        self.engine = engine
        self.public_group_uuid = ''
        self.create_tables()

    def _reset(self):
        '''
        Do a drop / create table to clear and reset the schema of all tables.
        '''
        # Do not run this function in production!
        db_metadata.drop_all(self.engine)
        self.create_tables()

    def create_tables(self):
        '''
        Create all CodaLab bundle tables if they do not already exist.
        '''
        db_metadata.create_all(self.engine)
        self._create_default_groups()

    def do_multirow_insert(self, connection, table, values):
        '''
        Insert multiple rows into the given table.

        This method may be overridden by models that use more powerful SQL dialects.
        '''
        # This is a lowest-common-denominator implementation of a multi-row insert.
        # It deals with a couple of SQL dialect issues:
        #   - Some dialects do not support empty inserts, so we test 'if values'.
        #   - Some dialects do not support multiple inserts in a single statement,
        #     which we deal with by using the DBAPI execute_many pattern.
        if values:
            connection.execute(table.insert(), values)

    def make_clause(self, key, value):
        if isinstance(value, (list, set, tuple)):
            if not value:
                return False
            return key.in_(value)
        elif isinstance(value, LikeQuery):
            return key.like(value)
        else:
            return key == value

    def make_kwargs_clause(self, table, kwargs):
        '''
        Return a list of bundles given a dict mapping table columns to values.
        If a value is a list, set, or tuple, produce an IN clause on that column.
        If a value is a LikeQuery, produce a LIKE clause on that column.
        '''
        clauses = [true()]
        for (key, value) in kwargs.iteritems():
            clauses.append(self.make_clause(getattr(table.c, key), value))
        return and_(*clauses)

    def get_bundle(self, uuid):
        '''
        Retrieve a bundle from the database given its uuid.
        Assume it's unique.
        '''
        bundles = self.batch_get_bundles(uuid=uuid)
        if not bundles:
            raise UsageError('Could not find bundle with uuid %s' % (uuid,))
        elif len(bundles) > 1:
            raise IntegrityError('Found multiple bundles with uuid %s' % (uuid,))
        return bundles[0]

    def get_bundle_names(self, uuids):
        '''
        Fetch the bundle names of the given uuids.
        Return {uuid: ..., name: ...}
        '''
        if len(uuids) == 0:
            return []
        with self.engine.begin() as connection:
            rows = connection.execute(select([
                cl_bundle_metadata.c.bundle_uuid,
                cl_bundle_metadata.c.metadata_value
            ]).where(
                and_(cl_bundle_metadata.c.metadata_key == 'name',
                     cl_bundle_metadata.c.bundle_uuid.in_(uuids))
            )).fetchall()
            return dict((row.bundle_uuid, row.metadata_value) for row in rows)

    def get_owner_ids(self, table, uuids):
        '''
        Fetch the owners of the given uuids (for either bundles or worksheets).
        Return {uuid: ..., owner_id: ...}
        '''
        if len(uuids) == 0:
            return []
        with self.engine.begin() as connection:
            rows = connection.execute(select([
                table.c.uuid,
                table.c.owner_id,
            ]).where(table.c.uuid.in_(uuids))).fetchall()
            return dict((row.uuid, row.owner_id) for row in rows)
    def get_bundle_owner_ids(self, uuids):
        return self.get_owner_ids(cl_bundle, uuids)
    def get_worksheet_owner_ids(self, uuids):
        return self.get_owner_ids(cl_worksheet, uuids)

    def get_children_uuids(self, uuids):
        '''
        Get all bundles that depend on the bundle with the given uuids.
        Return {parent_uuid: [child_uuid, ...], ...}
        '''
        with self.engine.begin() as connection:
            rows = connection.execute(select([
              cl_bundle_dependency.c.parent_uuid,
              cl_bundle_dependency.c.child_uuid,
            ]).where(cl_bundle_dependency.c.parent_uuid.in_(uuids))).fetchall()
        result = dict((uuid, []) for uuid in uuids)
        for row in rows:
            result[row.parent_uuid].append(row.child_uuid)
        return result

    def get_host_worksheet_uuids(self, bundle_uuids):
        '''
        Return list of worksheet uuids that contain the given bundle_uuids.
        bundle_uuids = ['0x12435']
        Return {'0x12435': [host_worksheet_uuid, ...], ...}
        '''
        with self.engine.begin() as connection:
            rows = connection.execute(select([
              cl_worksheet_item.c.worksheet_uuid,
              cl_worksheet_item.c.bundle_uuid,
            ]).where(cl_worksheet_item.c.bundle_uuid.in_(bundle_uuids))).fetchall()
        result = dict((uuid, []) for uuid in bundle_uuids)
        for row in rows:
            result[row.bundle_uuid].append(row.worksheet_uuid)
        return result

    def get_self_and_descendants(self, uuids, depth):
        '''
        Get all bundles that depend on bundles with the given uuids.
        depth = 1 gets only children
        '''
        frontier = uuids
        visited = list(frontier)
        while len(frontier) > 0 and depth > 0:
            # Get children of all nodes in frontier
            result = self.get_children_uuids(frontier)
            new_frontier = []
            for l in result.values():
                for uuid in l:
                    if uuid in visited:
                        continue
                    new_frontier.append(uuid)
                    visited.append(uuid)
            frontier = new_frontier
            depth -= 1
        return visited

    def search_bundle_uuids(self, user_id, worksheet_uuid, keywords):
        '''
        Return a list of uuids (in the appropriate order) matching the keywords.
        Each keyword is either:
        - <key>=<value>
        - .orphan: return bundles
        - .offset=<int>
        - .limit=<int>: maximum number of bundles to return
        - .count: just return the number
        - <general word>
        Keys are one of the following:
        - Bundle fields (e.g., uuid)
        - Metadata fields (e.g., time)
        - Special fields (e.g., dependencies)
        Values can be one of the following:
        - .sort: sort in increasing order
        - .sort-: sort by decreasing order
        - .count|.min|.max|.sum: aggregate
        Search only bundles which are readable by user_id.
        worksheet_uuid is not used right now.
        '''
        clauses = []
        offset = 0
        limit = 10
        count = False
        sort_key = [None]
        sum_key = [None]

        def make_condition(field, value):
            # Special
            if value == '.sort':
                sort_key[0] = field * 1
            elif value == '.sort-':
                # TODO: if field is not numeric, this doesn't work.
                # We should either detect whether the field is numeric.
                sort_key[0] = desc(field * 1)
            elif value == '.sum':
                sum_key[0] = field * 1
            else:
                # Ordinary value
                if '%' in value:
                    return field.like(value)
                else:
                    return field == value
            return true()

        shortcuts = {
            'type': 'bundle_type',
            'size': 'data_size',
            'worksheet': 'host_worksheet',
        }

        for keyword in keywords:
            keyword = keyword.replace('.*', '%')
            m = CONDITION_REGEX.match(keyword) # key=value
            clause = None
            if m:
                key, value = m.group(1), m.group(2)
                key = shortcuts.get(key, key)
                # Bundle fields
                if key == 'bundle_type':
                    clause = make_condition(cl_bundle.c.bundle_type, value)
                elif key == 'id':
                    clause = make_condition(cl_bundle.c.id, value)
                elif key == 'uuid':
                    clause = make_condition(cl_bundle.c.uuid, value)
                elif key == 'data_hash':
                    clause = make_condition(cl_bundle.c.data_hash, value)
                elif key == 'state':
                    clause = make_condition(cl_bundle.c.state, value)
                elif key == 'command':
                    clause = make_condition(cl_bundle.c.command, value)
                elif key == 'owner_id':
                    clause = make_condition(cl_bundle.c.owner_id, value)
                # Special fields
                elif key == 'dependency':
                    # Match uuid of dependency
                    condition = make_condition(cl_bundle_dependency.c.parent_uuid, value)
                    if condition == true():  # top-level
                        clause = and_(
                            cl_bundle_dependency.c.child_uuid == cl_bundle.c.uuid,
                            condition,
                        )
                    else:  # embedded
                        clause = cl_bundle.c.uuid.in_(select([cl_bundle_dependency.c.child_uuid]).where(condition))
                elif key.startswith('dependency/'):
                    _, name = key.split('/', 1)
                    condition = make_condition(cl_bundle_dependency.c.parent_uuid, value)
                    if condition == true():  # top-level
                        clause = and_(
                            cl_bundle_dependency.c.child_uuid == cl_bundle.c.uuid,  # Join constraint
                            cl_bundle_dependency.c.child_path == name,  # Match the 'type' of dependent (child_path)
                            condition,
                        )
                    else:  # embedded
                        clause = cl_bundle.c.uuid.in_(select([cl_bundle_dependency.c.child_uuid]).where(and_(
                            cl_bundle_dependency.c.child_path == name,  # Match the 'type' of dependent (child_path)
                            condition,
                        )))
                elif key == 'host_worksheet':
                    condition = make_condition(cl_worksheet_item.c.worksheet_uuid, value)
                    if condition == true():  # top-level
                        clause = and_(
                            cl_worksheet_item.c.bundle_uuid == cl_bundle.c.uuid,  # Join constraint
                            condition,
                        )
                    else:
                        clause = cl_bundle.c.uuid.in_(select([cl_worksheet_item.c.bundle_uuid]).where(condition))
                # Special functions
                elif key == '.offset':
                    offset = int(value)
                elif key == '.limit':
                    limit = int(value)
                # Otherwise, assume metadata.
                else:
                    condition = make_condition(cl_bundle_metadata.c.metadata_value, value)
                    if condition == true():  # top-level
                        clause = and_(
                            cl_bundle.c.uuid == cl_bundle_metadata.c.bundle_uuid,
                            cl_bundle_metadata.c.metadata_key == key,
                            condition,
                        )
                    else:  # embedded
                        clause = cl_bundle.c.uuid.in_(select([cl_bundle_metadata.c.bundle_uuid]).where(and_(
                            cl_bundle_metadata.c.metadata_key == key,
                            condition,
                        )))
            elif keyword == '.count':
                count = True
                limit = None
            elif keyword == '.orphan':
                # Get bundles that have host worksheets, and then take the complement.
                with_hosts = select([cl_bundle.c.uuid]).where(cl_bundle.c.uuid == cl_worksheet_item.c.bundle_uuid)
                clause = not_(cl_bundle.c.uuid.in_(with_hosts))
            else: # General keywords
                clause = []
                clause.append(cl_bundle.c.uuid.like('%' + keyword + '%'))
                clause.append(cl_bundle.c.command.like('%' + keyword + '%'))
                clause.append(cl_bundle.c.uuid.in_(select([cl_bundle_metadata.c.bundle_uuid]).where(
                    cl_bundle_metadata.c.metadata_value.like('%' + keyword + '%'),
                )))
                clause = or_(*clause)

            if clause is not None:
                clauses.append(clause)

        clause = and_(*clauses)

        if user_id != self.root_user_id:
            # Restrict to the bundles that we have access to.
            access_via_owner = (cl_bundle.c.owner_id == user_id)
            access_via_group = and_(
                cl_group_bundle_permission.c.object_uuid == cl_bundle.c.uuid,  # Join constraint (bundle)
                or_(  # Join constraint (group)
                    cl_group_bundle_permission.c.group_uuid == self.public_group_uuid,  # Public group
                    cl_group_bundle_permission.c.group_uuid.in_(select([cl_user_group.c.group_uuid]).where(cl_user_group.c.user_id == user_id))  # Private group
                ),
                cl_group_bundle_permission.c.permission >= GROUP_OBJECT_PERMISSION_READ,  # Match the uuid of the parent
            )
            clause = and_(clause, or_(access_via_owner, access_via_group))

        # Aggregate (sum)
        if sum_key[0] is not None:
            query = select([func.sum(sum_key[0])]).distinct().where(clause).offset(offset).limit(limit)
        else:
            query = select([cl_bundle.c.uuid]).distinct().where(clause).offset(offset).limit(limit)

        # Sort
        if sort_key[0] is not None:
            query = query.order_by(sort_key[0])

        # Count
        if count:
            query = query.count()

        #print 'QUERY', self._render_query(query)
        result = self._execute_query(query)
        if count or sum_key[0] is not None:  # Just returning a single number
            return result[0]
        #print 'RESULT', result
        return result

    def get_bundle_uuids(self, conditions, max_results):
        '''
        Returns a list of bundle_uuids that have match the conditions.
        Possible conditions on bundles: uuid, name, worksheet_uuid
        '''
        if 'uuid' in conditions:
            # Match the uuid only
            clause = self.make_clause(cl_bundle.c.uuid, conditions['uuid'])
            query = select([cl_bundle.c.uuid]).where(clause)
        elif 'name' in conditions:
            # Select name
            if conditions['name']:
                clause = and_(
                  cl_bundle_metadata.c.metadata_key == 'name',
                  self.make_clause(cl_bundle_metadata.c.metadata_value, conditions['name'])
                )
            else:
                clause = true()
            if conditions['worksheet_uuid']:
                # Select things on the given worksheet
                clause = and_(clause, self.make_clause(cl_worksheet_item.c.worksheet_uuid, conditions['worksheet_uuid']))
                clause = and_(clause, cl_worksheet_item.c.bundle_uuid == cl_bundle_metadata.c.bundle_uuid)  # Join
                query = select([cl_bundle_metadata.c.bundle_uuid, cl_worksheet_item.c.id]).distinct().where(clause)
                query = query.order_by(cl_worksheet_item.c.id.desc()).limit(max_results)
            else:
                if not conditions['name']:
                    raise UsageError('Nothing is specified')
                # Select from all bundles
                clause = and_(clause, cl_bundle.c.uuid == cl_bundle_metadata.c.bundle_uuid)  # Join
                query = select([cl_bundle.c.uuid]).where(clause)
                query = query.order_by(cl_bundle.c.id.desc()).limit(max_results)

        return self._execute_query(query)

    # Helper function: return string representing SQL query.
    def _render_query(self, query):
        query = query.compile()
        s = str(query)
        for k, v in query.params.items():
            s = s.replace(':' + k, str(v))
        return s

    def _execute_query(self, query):
        with self.engine.begin() as connection:
            rows = connection.execute(query).fetchall()
        return [row[0] for row in rows]

    def batch_get_bundles(self, **kwargs):
        '''
        Return a list of bundles given a SQLAlchemy clause on the cl_bundle table.
        '''
        clause = self.make_kwargs_clause(cl_bundle, kwargs)
        with self.engine.begin() as connection:
            bundle_rows = connection.execute(
              cl_bundle.select().where(clause)
            ).fetchall()
            if not bundle_rows:
                return []
            uuids = set(bundle_row.uuid for bundle_row in bundle_rows)
            dependency_rows = connection.execute(cl_bundle_dependency.select().where(
              cl_bundle_dependency.c.child_uuid.in_(uuids)
            )).fetchall()
            metadata_rows = connection.execute(cl_bundle_metadata.select().where(
              cl_bundle_metadata.c.bundle_uuid.in_(uuids)
            )).fetchall()

        # Make a dictionary for each bundle with both data and metadata.
        bundle_values = {row.uuid: dict(row) for row in bundle_rows}
        for bundle_value in bundle_values.itervalues():
            bundle_value['dependencies'] = []
            bundle_value['metadata'] = []
        for dep_row in dependency_rows:
            if dep_row.child_uuid not in bundle_values:
                raise IntegrityError('Got dependency %s without bundle' % (dep_row,))
            bundle_values[dep_row.child_uuid]['dependencies'].append(dep_row)
        for metadata_row in metadata_rows:
            if metadata_row.bundle_uuid not in bundle_values:
                raise IntegrityError('Got metadata %s without bundle' % (metadata_row,))
            bundle_values[metadata_row.bundle_uuid]['metadata'].append(metadata_row)

        # Construct and validate all of the retrieved bundles.
        sorted_values = sorted(bundle_values.itervalues(), key=lambda r: r['id'])
        bundles = [
          get_bundle_subclass(bundle_value['bundle_type'])(bundle_value)
          for bundle_value in sorted_values
        ]
        for bundle in bundles:
            bundle.validate()
        return bundles

    def batch_update_bundles(self, bundles, update, condition=None):
        '''
        Update a list of bundles given a dict mapping columns to new values and
        return True if all updates succeed. This method does NOT update metadata.

        If a condition is specified, only update bundles that satisfy the condition.

        In general, this method should only be used for programmatic updates, as in
        the bundle worker. It is provided as an efficient way to perform a simple
        update on many, but these updates are not validated.
        '''
        message = 'Illegal update: %s' % (update,)
        precondition('id' not in update and 'uuid' not in update, message)
        if bundles:
            bundle_ids = set(bundle.id for bundle in bundles)
            clause = cl_bundle.c.id.in_(bundle_ids)
            if condition:
                clause = and_(clause, self.make_kwargs_clause(cl_bundle, condition))
            with self.engine.begin() as connection:
                result = connection.execute(
                  cl_bundle.update().where(clause).values(update)
                )
                success = result.rowcount == len(bundle_ids)
                if success:
                    for bundle in bundles:
                        bundle.update_in_memory(update)
                return success
        return True

    def add_bundle_action(self, uuid, action):
        with self.engine.begin() as connection:
            connection.execute(cl_bundle_action.insert().values({"bundle_uuid": uuid, "action": action}))

    def add_bundle_actions(self, bundle_actions):
        with self.engine.begin() as connection:
            self.do_multirow_insert(connection, cl_bundle_action, bundle_actions)

    def pop_bundle_actions(self):
        with self.engine.begin() as connection:
            results = connection.execute(cl_bundle_action.select()).fetchall()  # Get the actions
            connection.execute(cl_bundle_action.delete())  # Delete all actions
            return [x for x in results]

    def save_bundle(self, bundle):
        '''
        Save a bundle. On success, sets the Bundle object's id from the result.
        '''
        bundle.validate()
        bundle_value = bundle.to_dict()
        dependency_values = bundle_value.pop('dependencies')
        metadata_values = bundle_value.pop('metadata')

        # Check to see if bundle is already present, as in a local 'cl cp'
        if not self.batch_get_bundles(uuid=bundle.uuid):
            with self.engine.begin() as connection:
                result = connection.execute(cl_bundle.insert().values(bundle_value))
                self.do_multirow_insert(connection, cl_bundle_dependency, dependency_values)
                self.do_multirow_insert(connection, cl_bundle_metadata, metadata_values)
                bundle.id = result.lastrowid


    def update_bundle(self, bundle, update):
        '''
        Update a bundle's columns and metadata in the database and in memory.
        The update is done as a diff: columns that do not appear in the update dict
        and metadata keys that do not appear in the metadata sub-dict are unaffected.

        This method validates all updates to the bundle, so it is appropriate
        to use this method to update bundles based on user input (eg: cl edit).
        '''
        message = 'Illegal update: %s' % (update,)
        precondition('id' not in update and 'uuid' not in update, message)
        # Apply the column and metadata updates in memory and validate the result.
        metadata_update = update.pop('metadata', {})
        bundle.update_in_memory(update)
        for (key, value) in metadata_update.iteritems():
            bundle.metadata.set_metadata_key(key, value)
        bundle.validate()
        # Construct clauses and update lists for updating certain bundle columns.
        if update:
            clause = cl_bundle.c.uuid == bundle.uuid
        if metadata_update:
            metadata_clause = and_(
              cl_bundle_metadata.c.bundle_uuid == bundle.uuid,
              cl_bundle_metadata.c.metadata_key.in_(metadata_update)
            )
            metadata_values = [
              row_dict for row_dict in bundle.to_dict().pop('metadata')
              if row_dict['metadata_key'] in metadata_update
            ]
        # Perform the actual updates.
        with self.engine.begin() as connection:
            if update:
                connection.execute(cl_bundle.update().where(clause).values(update))
            if metadata_update:
                connection.execute(cl_bundle_metadata.delete().where(metadata_clause))
                self.do_multirow_insert(connection, cl_bundle_metadata, metadata_values)

    def _check_not_running(self, uuids):
        # Make sure we don't delete running bundles.
        with self.engine.begin() as connection:
            rows = connection.execute(select([cl_bundle.c.uuid, cl_bundle.c.state]).where(cl_bundle.c.uuid.in_(uuids))).fetchall()
            running_uuids = [r.uuid for r in rows if r.state == State.RUNNING]
            if len(running_uuids) > 0:
                raise UsageError('Can\'t delete running bundles: %s' % ' '.join(running_uuids))

    def delete_bundles(self, uuids):
        '''
        Delete bundles with the given uuids.
        '''
        self._check_not_running(uuids)
        with self.engine.begin() as connection:
            # We must delete bundles rows in the opposite order that we create them
            # to avoid foreign-key constraint failures.
            connection.execute(cl_group_bundle_permission.delete().where(
                cl_group_bundle_permission.c.object_uuid.in_(uuids)
            ))
            connection.execute(cl_worksheet_item.delete().where(
                cl_worksheet_item.c.bundle_uuid.in_(uuids)
            ))
            connection.execute(cl_bundle_metadata.delete().where(
                cl_bundle_metadata.c.bundle_uuid.in_(uuids)
            ))
            connection.execute(cl_bundle_dependency.delete().where(
                cl_bundle_dependency.c.child_uuid.in_(uuids)
            ))
            connection.execute(cl_bundle.delete().where(
                cl_bundle.c.uuid.in_(uuids)
            ))

    def remove_data_hash_references(self, uuids):
        self._check_not_running(uuids)
        with self.engine.begin() as connection:
            connection.execute(cl_bundle.update().where(cl_bundle.c.uuid.in_(uuids)).values({'data_hash': None}))

    #############################################################################
    # Worksheet-related model methods follow!
    #############################################################################

    def get_worksheet(self, uuid, fetch_items):
        '''
        Get a worksheet given its uuid.
        '''
        worksheets = self.batch_get_worksheets(fetch_items=fetch_items, uuid=uuid)
        if not worksheets:
            raise UsageError('Could not find worksheet with uuid %s' % (uuid,))
        elif len(worksheets) > 1:
            raise IntegrityError('Found multiple workseets with uuid %s' % (uuid,))
        return worksheets[0]

    def batch_get_worksheets(self, fetch_items, **kwargs):
        '''
        Get a list of worksheets, all of which satisfy the clause given by kwargs.
        '''
        base_worksheet_uuid = kwargs.pop('base_worksheet_uuid', None)
        clause = self.make_kwargs_clause(cl_worksheet, kwargs)
        # Handle base_worksheet_uuid specially
        if base_worksheet_uuid:
            clause = and_(clause,
                cl_worksheet_item.c.subworksheet_uuid == cl_worksheet.c.uuid,
                cl_worksheet_item.c.worksheet_uuid == base_worksheet_uuid)

        with self.engine.begin() as connection:
            worksheet_rows = connection.execute(
              cl_worksheet.select().distinct().where(clause)
            ).fetchall()
            if not worksheet_rows:
                if base_worksheet_uuid != None:
                    # We didn't find any results restricting to base_worksheet_uuid,
                    # so do a global search
                    return self.batch_get_worksheets(fetch_items, **kwargs)
                return []
            # Fetch the items of all the worksheets
            if fetch_items:
                uuids = set(row.uuid for row in worksheet_rows)
                item_rows = connection.execute(cl_worksheet_item.select().where(
                  cl_worksheet_item.c.worksheet_uuid.in_(uuids)
                )).fetchall()
        # Make a dictionary for each worksheet with both its main row and its items.
        worksheet_values = {row.uuid: dict(row) for row in worksheet_rows}
        if fetch_items:
            for value in worksheet_values.itervalues():
                value['items'] = []
            for item_row in sorted(item_rows, key=item_sort_key):
                if item_row.worksheet_uuid not in worksheet_values:
                    raise IntegrityError('Got item %s without worksheet' % (item_row,))
                worksheet_values[item_row.worksheet_uuid]['items'].append(item_row)
        return [Worksheet(value) for value in worksheet_values.itervalues()]

    def list_worksheets(self, user_id=None):
        '''
        Return a list of row dicts, one per worksheet. These dicts do NOT contain
        ALL worksheet items; this method is meant to make it easy for a user to see
        their existing worksheets.
        '''
        cols_to_select = [cl_worksheet.c.id,
                          cl_worksheet.c.uuid,
                          cl_worksheet.c.name,
                          cl_worksheet.c.owner_id,
                          cl_group_worksheet_permission.c.permission]
        cols1 = cols_to_select[:4]
        cols1.extend([literal(GROUP_OBJECT_PERMISSION_ALL).label('permission')])
        if user_id == self.root_user_id:
            # query all worksheets
            stmt = select(cols1)
        elif user_id is None:
            # query for public worksheets (only used by the webserver when user is not logged in)
            stmt = select(cols_to_select).\
                where(cl_worksheet.c.uuid == cl_group_worksheet_permission.c.object_uuid).\
                where(cl_group_worksheet_permission.c.group_uuid == self.public_group_uuid)
        else:
            # 1) Worksheets owned by owner_id
            stmt1 = select(cols1).where(cl_worksheet.c.owner_id == user_id)

            # 2) Worksheets visible to owner_id or co-owned by owner_id
            stmt2_groups = select([cl_user_group.c.group_uuid]).\
                where(cl_user_group.c.user_id == user_id)
            # List worksheets where one of our groups has permission.
            stmt2 = select(cols_to_select).\
                where(cl_worksheet.c.uuid == cl_group_worksheet_permission.c.object_uuid).\
                where(or_(
                    cl_group_worksheet_permission.c.group_uuid.in_(stmt2_groups),
                    cl_group_worksheet_permission.c.group_uuid == self.public_group_uuid)).\
                where(cl_worksheet.c.owner_id != user_id)  # Avoid duplicates

            stmt = union(stmt1, stmt2)

        with self.engine.begin() as connection:
            rows = connection.execute(stmt).fetchall()
            if not rows:
                return []

        # Get permissions of the worksheets
        worksheet_uuids = [row.uuid for row in rows]
        uuid_group_permissions = self.batch_get_group_worksheet_permissions(worksheet_uuids)

        # Put the permissions into the worksheets
        row_dicts = []
        for row in sorted(rows, key=lambda item: item['id']):
            row = dict(row)
            row['group_permissions'] = uuid_group_permissions[row['uuid']]
            row_dicts.append(row)

        return row_dicts

    def save_worksheet(self, worksheet):
        '''
        Save the given (empty) worksheet to the database. On success, set its id.
        '''
        message = 'save_worksheet called with non-empty worksheet: %s' % (worksheet,)
        precondition(not worksheet.items, message)
        worksheet.validate()
        worksheet_value = worksheet.to_dict()
        with self.engine.begin() as connection:
            result = connection.execute(cl_worksheet.insert().values(worksheet_value))
            worksheet.id = result.lastrowid

    def add_worksheet_item(self, worksheet_uuid, item):
        '''
        Appends a new item to the end of the given worksheet. The item should be
        a (bundle_uuid, value, type) pair, where the bundle_uuid may be None and the
        value must be a string.
        '''
        (bundle_uuid, subworksheet_uuid, value, type) = item
        if value == None: value = ''  # TODO: change tables.py to allow nulls
        item_value = {
          'worksheet_uuid': worksheet_uuid,
          'bundle_uuid': bundle_uuid,
          'subworksheet_uuid': subworksheet_uuid,
          'value': value,
          'type': type,
          'sort_key': None,
        }
        with self.engine.begin() as connection:
            connection.execute(cl_worksheet_item.insert().values(item_value))

    def add_shadow_worksheet_items(self, old_bundle_uuid, new_bundle_uuid):
        '''
        For each occurrence of old_bundle_uuid in any worksheet, add
        new_bundle_uuid right after it (a shadow).
        '''
        with self.engine.begin() as connection:
            # Find all the worksheet_items that old_bundle_uuid appears in
            query = select([cl_worksheet_item.c.worksheet_uuid, cl_worksheet_item.c.sort_key]).where(cl_worksheet_item.c.bundle_uuid == old_bundle_uuid)
            old_items = connection.execute(query)
            #print 'add_shadow_worksheet_items', old_items

            # Go through and insert a worksheet item with new_bundle_uuid after
            # each of the old items.
            new_items = []
            for old_item in old_items:
                new_item = {
                  'worksheet_uuid': old_item.worksheet_uuid,
                  'bundle_uuid': new_bundle_uuid,
                  'type': worksheet_util.TYPE_BUNDLE,
                  'value': '',  # TODO: replace with None once we change tables.py
                  'sort_key': old_item.sort_key,  # Can't really do after, so use the same value.
                }
                new_items.append(new_item)
                connection.execute(cl_worksheet_item.insert().values(new_item))
            # sqlite doesn't support batch insertion
            #connection.execute(cl_worksheet_item.insert().values(new_items))

    def update_worksheet(self, worksheet_uuid, last_item_id, length, new_items):
        '''
        Updates the worksheet with the given uuid. If there were exactly
        `last_length` items with database id less than `last_id`, replaces them all
        with the items in new_items. Does NOT affect items in this worksheet with
        database id greater than last_id.

        Does NOT affect items that were added to the worksheet in between the
        time it was retrieved and it was updated.

        If this worksheet were updated between the time it was retrieved and
        updated, this method will raise a UsageError.
        '''
        clause = and_(
          cl_worksheet_item.c.worksheet_uuid == worksheet_uuid,
          cl_worksheet_item.c.id <= last_item_id,
        )
        # See codalab.objects.worksheet for an explanation of the sort_key protocol.
        # We need to produce sort keys here that are strictly upper-bounded by the
        # last known item id in this worksheet, and which monotonically increase.
        # The expression last_item_id + i - len(new_items) works. It can produce
        # negative sort keys, but that's fine.
        new_item_values = [{
          'worksheet_uuid': worksheet_uuid,
          'bundle_uuid': bundle_uuid,
          'subworksheet_uuid': subworksheet_uuid,
          'value': value,
          'type': type,
          'sort_key': (last_item_id + i - len(new_items)),
        } for (i, (bundle_uuid, subworksheet_uuid, value, type)) in enumerate(new_items)]
        with self.engine.begin() as connection:
            result = connection.execute(cl_worksheet_item.delete().where(clause))
            message = 'Found extra items for worksheet %s' % (worksheet_uuid,)
            precondition(result.rowcount <= length, message)
            if result.rowcount < length:
                raise UsageError('Worksheet %s was updated concurrently!' % (worksheet_uuid,))
            self.do_multirow_insert(connection, cl_worksheet_item, new_item_values)

    def rename_worksheet(self, worksheet, name):
        '''
        Update the given worksheet's name.
        '''
        worksheet.name = name
        worksheet.validate()
        with self.engine.begin() as connection:
            connection.execute(cl_worksheet.update().where(
              cl_worksheet.c.uuid == worksheet.uuid
            ).values({'name': name}))

    def chown_worksheet(self, worksheet, owner_id):
        '''
        Update the given worksheet's owner_id.
        '''
        worksheet.owner_id = owner_id
        worksheet.validate()
        with self.engine.begin() as connection:
            connection.execute(cl_worksheet.update().where(
              cl_worksheet.c.uuid == worksheet.uuid
            ).values({'owner_id': owner_id}))

    def delete_worksheet(self, worksheet_uuid):
        '''
        Delete the worksheet with the given uuid.
        '''
        with self.engine.begin() as connection:
            connection.execute(cl_group_worksheet_permission.delete().where(
                cl_group_worksheet_permission.c.object_uuid == worksheet_uuid
            ))
            connection.execute(cl_worksheet_item.delete().where(
                cl_worksheet_item.c.worksheet_uuid == worksheet_uuid
            ))
            connection.execute(cl_worksheet_item.delete().where(
                cl_worksheet_item.c.subworksheet_uuid == worksheet_uuid
            ))
            connection.execute(cl_worksheet.delete().where(
                cl_worksheet.c.uuid == worksheet_uuid
            ))

    #############################################################################
    # Commands related to groups and permissions follow!
    #############################################################################

    def _create_default_groups(self):
        '''
        Create system-defined groups. This is called by create_tables.
        '''
        groups = self.batch_get_groups(name='public', user_defined=False)
        if len(groups) == 0:
            group_dict = self.create_group({'uuid': spec_util.generate_uuid(),
                                            'name': 'public',
                                            'owner_id': None,
                                            'user_defined': False})
        else:
            group_dict = groups[0]
        self.public_group_uuid = group_dict['uuid']

    def list_groups(self, owner_id):
        '''
        Return a list of row dicts --one per group-- for the given owner.
        '''
        with self.engine.begin() as connection:
            rows = connection.execute(cl_group.select().where(
                cl_group.c.owner_id == owner_id
            )).fetchall()
        return [dict(row) for row in sorted(rows, key=lambda row: row.id)]

    def create_group(self, group_dict):
        '''
        Create the group specified by the given row dict.
        '''
        with self.engine.begin() as connection:
            result = connection.execute(cl_group.insert().values(group_dict))
            group_dict['id'] = result.lastrowid
        return group_dict

    def batch_get_groups(self, **kwargs):
        '''
        Get a list of groups, all of which satisfy the clause given by kwargs.
        '''
        clause = self.make_kwargs_clause(cl_group, kwargs)
        with self.engine.begin() as connection:
            rows = connection.execute(
              cl_group.select().where(clause)
            ).fetchall()
            if not rows:
                return []
        values = {row.uuid: dict(row) for row in rows}
        return [value for value in values.itervalues()]

    def batch_get_all_groups(self, spec_filters, group_filters, user_group_filters):
        '''
        Get a list of groups by querying the group table and/or the user_group table.
        Take the union of the two results.  This method performs the general query:
        - q0: use spec_filters on the public group
        - q1: use spec_filters and group_filters on group
        - q2: use spec_filters and user_group_filters on user_group
        return union(q0, q1, q2)
        '''
        fetch_cols = [cl_group.c.uuid, cl_group.c.name, cl_group.c.owner_id]
        fetch_cols0 = fetch_cols + [cl_group.c.owner_id.label('user_id'), literal(False).label('is_admin')]
        fetch_cols1 = fetch_cols + [cl_group.c.owner_id.label('user_id'), literal(True).label('is_admin')]
        fetch_cols2 = fetch_cols + [cl_user_group.c.user_id, cl_user_group.c.is_admin]

        q0 = None
        q1 = None
        q2 = None

        if spec_filters:
            spec_clause = self.make_kwargs_clause(cl_group, spec_filters)
            q0 = select(fetch_cols0).where(spec_clause)
            q1 = select(fetch_cols1).where(spec_clause)
            q2 = select(fetch_cols2).where(spec_clause).where(cl_group.c.uuid == cl_user_group.c.group_uuid)
        if True:
            if q0 is None:
                q0 = select(fetch_cols0)
            q0 = q0.where(cl_group.c.uuid == self.public_group_uuid)
        if group_filters:
            group_clause = self.make_kwargs_clause(cl_group, group_filters)
            if q1 is None:
                q1 = select(fetch_cols1)
            q1 = q1.where(group_clause)
        if user_group_filters:
            user_group_clause = self.make_kwargs_clause(cl_user_group, user_group_filters)
            if q2 is None:
                q2 = select(fetch_cols2).where(cl_group.c.uuid == cl_user_group.c.group_uuid)
            q2 = q2.where(user_group_clause)

        # Union
        q0 = union(*filter(lambda q : q is not None, [q0, q1, q2]))

        with self.engine.begin() as connection:
            rows = connection.execute(q0).fetchall()
            if not rows:
                return []
            for i, row in enumerate(rows):
                row = dict(row)
                # TODO: remove these conversions once database schema is changed from int to str
                if isinstance(row['user_id'], int): row['user_id'] = str(row['user_id'])
                if isinstance(row['owner_id'], int): row['owner_id'] = str(row['owner_id'])
                rows[i] = row
            values = {row['uuid']: dict(row) for row in rows}
            return [value for value in values.itervalues()]

    def delete_group(self, uuid):
        '''
        Delete the group with the given uuid.
        '''
        with self.engine.begin() as connection:
            connection.execute(cl_group_bundle_permission.delete().\
                where(cl_group_bundle_permission.c.group_uuid == uuid)
            )
            connection.execute(cl_group_worksheet_permission.delete().\
                where(cl_group_worksheet_permission.c.group_uuid == uuid)
            )
            connection.execute(cl_user_group.delete().\
                where(cl_user_group.c.group_uuid == uuid)
            )
            connection.execute(cl_group.delete().where(
              cl_group.c.uuid == uuid
            ))

    def add_user_in_group(self, user_id, group_uuid, is_admin):
        '''
        Add user as a member of a group.
        '''
        row = {'group_uuid': group_uuid, 'user_id': user_id, 'is_admin': is_admin}
        with self.engine.begin() as connection:
            result = connection.execute(cl_user_group.insert().values(row))
            row['id'] = result.lastrowid
        return row

    def delete_user_in_group(self, user_id, group_uuid):
        '''
        Add user as a member of a group.
        '''
        with self.engine.begin() as connection:
            connection.execute(cl_user_group.delete().\
                where(cl_user_group.c.user_id == user_id).\
                where(cl_user_group.c.group_uuid == group_uuid)
            )

    def update_user_in_group(self, user_id, group_uuid, is_admin):
        '''
        Add user as a member of a group.
        '''
        with self.engine.begin() as connection:
            connection.execute(cl_user_group.update().\
                where(cl_user_group.c.user_id == user_id).\
                where(cl_user_group.c.group_uuid == group_uuid).\
                values({'is_admin': is_admin}))

    def batch_get_user_in_group(self, **kwargs):
        '''
        Return list of user-group entries matching the specified |kwargs|.
        Can be used to get groups for a user or users in a group.
        Examples: user_id=..., group_uuid=...
        '''
        clause = self.make_kwargs_clause(cl_user_group, kwargs)
        with self.engine.begin() as connection:
            rows = connection.execute(
              cl_user_group.select().where(clause)
            ).fetchall()
            if not rows:
                return []
        return [dict(row) for row in rows]

    # Helper function: return list of group uuids that |user_id| is in.
    def _get_user_groups(self, user_id):
        groups = [self.public_group_uuid]  # Everyone is in the public group implicitly.
        if user_id != None:
            groups += [row['group_uuid'] for row in self.batch_get_user_in_group(user_id=user_id)]
        return groups

    def add_permission(self, table, group_uuid, object_uuid, permission):
        '''
        Add specified permission for the given (group, object) pair.
        '''
        row = {'group_uuid': group_uuid, 'object_uuid': object_uuid, 'permission': permission}
        with self.engine.begin() as connection:
            result = connection.execute(table.insert().values(row))
            row['id'] = result.lastrowid
        return row
    def add_bundle_permission(self, group_uuid, bundle_uuid, permission):
        self.add_permission(cl_group_bundle_permission, group_uuid, bundle_uuid, permission)
    def add_worksheet_permission(self, group_uuid, worksheet_uuid, permission):
        self.add_permission(cl_group_worksheet_permission, group_uuid, worksheet_uuid, permission)

    def delete_permission(self, table, group_uuid, object_uuid):
        '''
        Delete permissions for the given (group, object) pair.
        '''
        with self.engine.begin() as connection:
            connection.execute(table.delete(). \
                where(table.c.group_uuid == group_uuid). \
                where(table.c.object_uuid == object_uuid)
            )
    def delete_bundle_permission(self, group_uuid, bundle_uuid):
        self.delete_permission(cl_group_bundle_permission, group_uuid, bundle_uuid)
    def delete_worksheet_permission(self, group_uuid, worksheet_uuid):
        self.delete_permission(cl_group_worksheet_permission, group_uuid, worksheet_uuid)

    def update_permission(self, table, group_uuid, object_uuid, permission):
        '''
        Update permission for the given (group, object) pair.
        There should be one.
        '''
        with self.engine.begin() as connection:
            connection.execute(table.update(). \
                where(table.c.group_uuid == group_uuid). \
                where(table.c.object_uuid == object_uuid). \
                values({'permission': permission}))
    def update_bundle_permission(self, group_uuid, bundle_uuid, permission):
        self.update_permission(cl_group_bundle_permission, group_uuid, bundle_uuid, permission)
    def update_worksheet_permission(self, group_uuid, worksheet_uuid, permission):
        self.update_permission(cl_group_worksheet_permission, group_uuid, worksheet_uuid, permission)

    def batch_get_group_permissions(self, table, object_uuids):
        '''
        Return map from object_uuid to list of {group_uuid: ..., group_name: ..., permission: ...}
        '''
        with self.engine.begin() as connection:
            rows = connection.execute(select([table, cl_group.c.name])
                .where(table.c.group_uuid == cl_group.c.uuid)
                .where(table.c.object_uuid.in_(object_uuids))
            ).fetchall()
            result = collections.defaultdict(list)  # object_uuid => list of rows
            for row in rows:
                result[row.object_uuid].append({'group_uuid': row.group_uuid, 'group_name': row.name, 'permission': row.permission})
            return result
    def batch_get_group_bundle_permissions(self, bundle_uuids):
        return self.batch_get_group_permissions(cl_group_bundle_permission, bundle_uuids)
    def batch_get_group_worksheet_permissions(self, worksheet_uuids):
        return self.batch_get_group_permissions(cl_group_worksheet_permission, worksheet_uuids)

    def get_group_permissions(self, table, object_uuid):
        '''
        Return list of {group_uuid: ..., group_name: ..., permission: ...} entries for the given object.
        '''
        return self.batch_get_group_permissions(table, [object_uuid])[object_uuid]
    def get_group_bundle_permissions(self, bundle_uuid):
        return self.get_group_permissions(cl_group_bundle_permission, bundle_uuid)
    def get_group_worksheet_permissions(self, worksheet_uuid):
        return self.get_group_permissions(cl_group_worksheet_permission, worksheet_uuid)

    def get_group_permission(self, table, group_uuid, object_uuid):
        '''
        Get permission for the given (group, object) pair.
        '''
        for row in self.get_group_permissions(table, object_uuid):
            if row['group_uuid'] == group_uuid:
                return row['permission']
        return GROUP_OBJECT_PERMISSION_NONE
    def get_group_bundle_permission(self, group_uuid, bundle_uuid):
        return self.get_group_permission(cl_group_bundle_permission, group_uuid, bundle_uuid)
    def get_group_worksheet_permission(self, group_uuid, worksheet_uuid):
        return self.get_group_permission(cl_group_worksheet_permission, group_uuid, worksheet_uuid)

    def get_user_permissions(self, table, user_id, object_uuids, owner_ids):
        '''
        Gets the set of permissions granted to the given user on the given objects.
        owner_ids: map from object_uuid to owner_id.
        Return: map from object_uuid to permission.

        Use user_id = None to check the set of permissions of an anonymous user.
        To compute this, look at the groups that the user belongs to.
        '''
        object_permissions = dict((object_uuid, GROUP_OBJECT_PERMISSION_NONE) for object_uuid in object_uuids)

        remaining_object_uuids = []
        for object_uuid in object_uuids:
            owner_id = owner_ids.get(object_uuid)
            # Owner and root has all permissions.
            if user_id == owner_id or user_id == self.root_user_id:
                object_permissions[object_uuid] = GROUP_OBJECT_PERMISSION_ALL
            else:
                remaining_object_uuids.append(object_uuid)

        if len(remaining_object_uuids) > 0:
            result = self.batch_get_group_permissions(table, remaining_object_uuids)
            user_groups = self._get_user_groups(user_id)
            for object_uuid, permissions in result.items():
                for row in permissions:
                    if row['group_uuid'] in user_groups:
                        object_permissions[object_uuid] = max(object_permissions[object_uuid], row['permission'])
        return object_permissions
    def get_user_bundle_permissions(self, user_id, bundle_uuids, owner_ids):
        return self.get_user_permissions(cl_group_bundle_permission, user_id, bundle_uuids, owner_ids) 
    def get_user_worksheet_permissions(self, user_id, worksheet_uuids, owner_ids):
        return self.get_user_permissions(cl_group_worksheet_permission, user_id, worksheet_uuids, owner_ids) 
