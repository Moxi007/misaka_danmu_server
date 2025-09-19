import {
  Button,
  Card,
  Form,
  Input,
  List,
  message,
  Modal,
  Switch,
  Tag,
  Tooltip,
} from 'antd'
import { useEffect, useState, useRef } from 'react'
import { getMetaData, getProviderConfig, setMetaData, setProviderConfig } from '../../../apis'
import { MyIcon } from '@/components/MyIcon'
import {
  closestCorners,
  DndContext,
  DragOverlay,
  MouseSensor,
  TouchSensor,
  useSensor,
  useSensors,
} from '@dnd-kit/core'
import {
  SortableContext,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { ContainerOutlined } from '@ant-design/icons'
import { useMessage } from '../../../MessageContext'

const SortableItem = ({ item, index, handleChangeStatus, onConfig }) => {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({
    // 修正：始终使用 providerName 作为唯一 ID
    id: item.providerName,
    data: {
      item,
      index,
    },
  })

  // 拖拽样式
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
    cursor: 'grab',
    touchAction: 'none', // 关键：阻止浏览器默认触摸行为
    userSelect: 'none', // 防止拖拽时选中文本
    ...(isDragging && { cursor: 'grabbing' }),
  }

  return (
    <List.Item ref={setNodeRef} style={style} {...attributes}>
      {/* 保留你原有的列表项渲染逻辑 */}
      <div className="w-full flex items-center justify-between">
        {/* 左侧添加拖拽手柄 */}
        <div className="flex items-center gap-2">
          <div {...listeners} style={{ cursor: 'grab' }}>
            <MyIcon icon="drag" size={24} />
          </div>
          <div>{item.providerName}</div>
        </div>
        <div className="flex items-center justify-around gap-3">
          {/* 新增：配置按钮 */}
          <div onClick={onConfig} className="cursor-pointer">
            <MyIcon icon="setting" size={24} />
          </div>
          {item.status !== '未配置' && (
            <Tooltip title={item.status} trigger={['click', 'hover']}>
              <ContainerOutlined
                style={{
                  color: item.status?.includes('失败')
                    ? 'var(--color-red-400)'
                    : 'var(--color-green-400)',
                }}
              />
            </Tooltip>
          )}
          {item.isAuxSearchEnabled ? (
            <Tag color="green">已启用</Tag>
          ) : (
            <Tag color="red">未启用</Tag>
          )}
          {item.providerName !== 'tmdb' ? (
            <Tooltip title="切换启用状态">
              <div onClick={handleChangeStatus}>
                <MyIcon icon="exchange" size={24} />
              </div>
            </Tooltip>
          ) : (
            <div className="w-6"></div>
          )}
        </div>
      </div>
    </List.Item>
  )
}

export const Metadata = () => {
  const [loading, setLoading] = useState(true)
  const [list, setList] = useState([])
  const [activeItem, setActiveItem] = useState(null)
  const dragOverlayRef = useRef(null)
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [selectedSource, setSelectedSource] = useState(null)
  const [form] = Form.useForm()
  const [confirmLoading, setConfirmLoading] = useState(false)

  const messageApi = useMessage()

  const sensors = useSensors(
    useSensor(MouseSensor, {
      activationConstraint: {
        distance: 5,
      },
    }),
    useSensor(TouchSensor, {
      activationConstraint: {
        distance: 8,
        delay: 100,
      },
    })
  )

  const fetchInfo = () => {
    setLoading(true)
    getMetaData()
      .then(res => {
        setList(res.data ?? [])
      })
      .finally(() => {
        setLoading(false)
      })
  }

  useEffect(fetchInfo, [])

  useEffect(() => {
    if (isModalOpen && selectedSource?.providerName) {
      // 重置表单以防显示旧数据
      form.resetFields()
      getProviderConfig({ providerName: selectedSource.providerName })
        .then(res => {
          form.setFieldsValue({
            ...res.data,
            logRawResponses: res.data.logRawResponses ?? false,
          })
        })
        .catch(() => {
          messageApi.error('获取配置失败')
        })
    }
  }, [isModalOpen, selectedSource, form, messageApi])

  const handleDragEnd = event => {
    const { active, over } = event

    // 拖拽无效或未改变位置
    if (!over || active.id === over.id) {
      setActiveItem(null)
      return
    }

    // 找到原位置和新位置
    const activeIndex = list.findIndex(
      item => item.providerName === active.data.current.item.providerName
    )
    const overIndex = list.findIndex(
      item => item.providerName === over.data.current.item.providerName
    )

    if (activeIndex !== -1 && overIndex !== -1) {
      // 1. 重新排列数组
      const newList = [...list]
      const [movedItem] = newList.splice(activeIndex, 1)
      newList.splice(overIndex, 0, movedItem)

      // 2. 重新计算所有项的display_order（从1开始连续编号）
      const updatedList = newList.map((item, index) => ({
        ...item,
        displayOrder: index + 1, // 排序值从1开始
      }))

      // 3. 更新状态
      setList(updatedList)
      // 修正：只发送必要的字段，避免发送status等只读字段
      const payload = updatedList.map(item => ({
        providerName: item.providerName,
        isAuxSearchEnabled: item.isAuxSearchEnabled,
        displayOrder: item.displayOrder,
      }))
      setMetaData(payload)
      messageApi.success(
        `已更新排序，${movedItem.providerName} 移动到位置 ${overIndex + 1}`
      )
    }

    setActiveItem(null)
  }

  // 处理拖拽开始
  const handleDragStart = event => {
    const { active } = event
    // 找到当前拖拽的项
    const item = list.find(item => item.providerName === active.id)
    setActiveItem(item)
  }

  const handleChangeStatus = item => {
    const newList = list.map(it => {
      if (it.providerName === item.providerName) {
        return {
          ...it,
          isAuxSearchEnabled: !it.isAuxSearchEnabled,
        }
      } else {
        return it
      }
    })
    setList(newList)
    const payload = newList.map(item => ({
      providerName: item.providerName,
      isAuxSearchEnabled: item.isAuxSearchEnabled,
      displayOrder: item.displayOrder,
    }))
    setMetaData(payload)
  }

  const handleSaveSettings = async () => {
    try {
      setConfirmLoading(true)
      const values = await form.validateFields()
      await setProviderConfig(selectedSource.providerName, {
        ...values,
      })
      messageApi.success('保存成功')
      setIsModalOpen(false)
      // 成功后刷新列表以更新状态
      fetchInfo()
    } catch (error) {
      messageApi.error(`保存失败: ${error.message || '未知错误'}`)
    } finally {
      setConfirmLoading(false)
    }
  }

  const renderDragOverlay = () => {
    if (!activeItem) return null

    return (
      <div ref={dragOverlayRef} style={{ width: '100%', maxWidth: '100%' }}>
        <List.Item
          style={{
            boxShadow: '0 4px 12px rgba(0, 0, 0, 0.15)',
            opacity: 0.9,
          }}
        >
          <div className="w-full flex items-center justify-between">
            <div className="flex items-center gap-2">
              <MyIcon icon="drag" size={24} />
              <div>{activeItem.providerName}</div>
            </div>
            <div className="flex items-center justify-around gap-4">
              {activeItem.status !== '未配置' && (
                <Tooltip title={activeItem.status}>
                  <ContainerOutlined
                    style={{
                      color: activeItem.status?.includes('失败')
                        ? 'var(--color-red-400)'
                        : 'var(--color-green-400)',
                    }}
                  />
                </Tooltip>
              )}
              {activeItem.isAuxSearchEnabled ? (
                <Tag color="green">已启用</Tag>
              ) : (
                <Tag color="red">未启用</Tag>
              )}
              {activeItem.providerName !== 'tmdb' ? (
                <div>
                  <MyIcon icon="exchange" size={24} />
                </div>
              ) : (
                <div className="w-6"></div>
              )}
            </div>
          </div>
        </List.Item>
      </div>
    )
  }

  return (
    <div className="my-6">
      <Card loading={loading} title="元信息搜索源">
        <DndContext
          sensors={sensors}
          collisionDetection={closestCorners}
          onDragStart={handleDragStart}
          onDragEnd={handleDragEnd}
        >
          <SortableContext
            strategy={verticalListSortingStrategy}
            items={list.map(item => item.providerName)}
          >
            <List
              itemLayout="vertical"
              size="large"
              dataSource={list}
              renderItem={(item, index) => (
                <SortableItem
                  key={item.providerName}
                  item={item}
                  index={index}
                  handleChangeStatus={() => handleChangeStatus(item)}
                  onConfig={() => {
                    setSelectedSource(item)
                    setIsModalOpen(true)
                  }}
                />
              )}
            />
          </SortableContext>

          {/* 拖拽覆盖层 */}
          <DragOverlay>{renderDragOverlay()}</DragOverlay>
        </DndContext>
      </Card>
      <Modal
        title={`配置: ${selectedSource?.providerName}`}
        open={isModalOpen}
        onOk={handleSaveSettings}
        onCancel={() => setIsModalOpen(false)}
        confirmLoading={confirmLoading}
        destroyOnClose
        forceRender
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{ logRawResponses: false }}
        >
          <div className="my-4">
            请为 {selectedSource?.providerName} 源填写以下配置信息。
          </div>
          <div className="flex items-center justify-start flex-wrap md:flex-nowrap gap-2 mb-4">
            <Form.Item
              name="logRawResponses"
              label="记录原始响应"
              valuePropName="checked"
              className="min-w-[100px] shrink-0 !mb-0"
            >
              <Switch />
            </Form.Item>
            <div className="w-full text-gray-500">
              启用后，此源的所有API请求的原始响应将被记录到{' '}
              <code>config/logs/metadata_responses.log</code> 文件中，用于调试。
            </div>
        </div>
          {/* 修正：根据后端返回的 isFailoverSource 标志来决定是否显示此开关 */}
          {form.getFieldValue('isFailoverSource') && (
            <div className="flex items-center justify-start flex-wrap md:flex-nowrap gap-2 mb-4">
              <Form.Item
                name="forceAuxSearchEnabled"
                label="强制辅助搜索"
                valuePropName="checked"
                className="min-w-[100px] shrink-0 !mb-0"
              >
                <Switch />
              </Form.Item>
              <div
                className="w-full text-gray-500"
                title="启用后，在搜索时，此源将作为一个补充搜索源。如果其他弹幕源没有找到结果，或结果不佳，此源的结果将作为备选项显示在搜索结果中。"
              >
                启用后，此源将作为补充搜索源。当其他弹幕源结果不佳时，其结果将作为备选项显示。
              </div>
            </div>
          )}
        </Form>
      </Modal>
    </div>
  )
}
