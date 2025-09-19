import { Tabs } from 'antd'
import { TokenManage } from './components/TokenManage'
import { OutputManage } from './components/OutputManage'
import { MatchFallbackSetting } from './components/MatchFallbackSetting'
import { useNavigate, useSearchParams } from 'react-router-dom'

export const Bullet = () => {
  const [searchParams] = useSearchParams()
  const key = searchParams.get('key') || 'token'
  const navigate = useNavigate()

  return (
    <Tabs
      defaultActiveKey={key}
      items={[
        {
          label: 'Token管理',
          key: 'token',
          children: <TokenManage />,
        },
        {
          label: '弹幕输出控制',
          key: 'output',
          children: <OutputManage />,
        },
        {
          label: '设置',
          key: 'fallback',
          children: <MatchFallbackSetting />,
        },
      ]}
      onChange={key => {
        navigate(`/bullet?key=${key}`, {
          replace: true,
        })
      }}
    />
  )
}
